import asyncio
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

import streamlit as st
from langchain_core.messages import ToolMessage
from streamlit_agraph import Config, Edge, Node, agraph

from agent.graph import build_query_agent
from agent.ingest_graph import build_ingestion_agent
from ingestion.diff import DiffEngine
from ingestion.github import clone_repo, is_github_url
from ingestion.loader import (
    create_ingestion,
    delete_ingestion,
    get_all_ingestions,
    get_db_client,
    get_ingestions_for_repo,
    load_file,
)

# ── Colours ────────────────────────────────────────────────────────────────
REPO_COLOR   = "#E74C3C"
FOLDER_COLOR = "#F39C12"
FILE_COLOR   = "#4C8BF5"
FUNC_COLOR   = "#9B59B6"
CLASS_COLOR  = "#27AE60"

DIFF_COLORS = {
    "green":  "#1DB954",
    "yellow": "#F1C40F",
    "red":    "#FF4444",
}

_PATH_RE = re.compile(
    r"`((?:/[\w./-]+\.\w{1,6})(?::\d+)?)`"
    r"|(?<![\[(`/\w:])((?:/[\w./-]+\.\w{1,6})(?::\d+)?)"
    r"|(?<![\[(`/\w.])((?:[\w.-]+/)+[\w.-]+\.\w{1,6}(?::\d+)?)"
)


def _linkify_paths(text: str, repo_path: str = "") -> str:
    def _make_link(raw: str, original: str) -> str:
        path_part, _, line_part = raw.partition(":")
        if path_part.startswith("/"):
            abs_path = path_part
        elif repo_path:
            abs_path = f"{repo_path.rstrip('/')}/{path_part}"
        else:
            return original
        url = f"vscode://file{abs_path}" + (f":{line_part}" if line_part else "")
        return f"[`{raw}`]({url})"

    def _replace(m: re.Match) -> str:
        raw = m.group(1) or m.group(2) or m.group(3)
        return _make_link(raw, m.group(0))

    return _PATH_RE.sub(_replace, text)


# ── Background ingestion thread ────────────────────────────────────────────

def _run_ingestion(
    repo_path: str,
    disk_path: str,
    github_url: str | None,
    prev_ingestion_id: str | None,
    thread_id: str,
    stop_event: threading.Event,
    progress: dict,
    diff_highlights: dict,
    diff_status_log: list,
    cleanup_fn,
) -> None:
    try:
        # 1. Create ingestion record in DB
        async def _create():
            async with get_db_client() as db:
                return await create_ingestion(db, repo_path, github_url)

        ingestion_id = asyncio.run(_create())
        progress["ingestion_id"] = ingestion_id

        # 2. Run diff if we have a previous version
        if prev_ingestion_id:
            diff_status_log.append("Computing diff…")

            async def _run_diff():
                async with get_db_client() as db:
                    async for event in DiffEngine.run(disk_path, prev_ingestion_id, db):
                        diff_highlights[event["node_id"]] = event["status"]
                        diff_status_log.append(
                            f"{event['status'].upper()}: {Path(event['path']).name}"
                        )

            asyncio.run(_run_diff())
            diff_status_log.append(f"Diff complete — {len(diff_highlights)} nodes highlighted.")

        # 3. Run LangGraph ingestion agent
        progress["status"] = "running"
        agent = build_ingestion_agent()
        config = {"configurable": {"thread_id": thread_id}}
        init_state = {
            "repo_path": repo_path,
            "disk_path": disk_path,
            "ingestion_id": ingestion_id,
            "all_files": [],
            "processed_files": [],
            "current_file": "",
        }

        for chunk in agent.stream(init_state, config, stream_mode="values"):
            processed = chunk.get("processed_files") or []
            all_files = chunk.get("all_files") or []
            progress["processed"] = len(processed)
            progress["total"] = len(all_files)

            current = chunk.get("current_file", "")
            if current:
                # Clear corresponding prev-version highlight as we re-ingest
                if prev_ingestion_id:
                    prev_node_id = (
                        "file:"
                        + hashlib.md5((current + prev_ingestion_id).encode()).hexdigest()[:12]
                    )
                    diff_highlights.pop(prev_node_id, None)
                diff_status_log.append(f"Ingested: {Path(current).name}")

            if stop_event.is_set():
                progress["status"] = "stopped"
                return

        # 4. Clear diff_status from prev version's nodes in DB
        if prev_ingestion_id:
            async def _clear_diff_status():
                async with get_db_client() as db:
                    await db.query(
                        "UPDATE file SET diff_status = NONE WHERE ingestion_id = $iid",
                        {"iid": prev_ingestion_id},
                    )
            asyncio.run(_clear_diff_status())

        diff_highlights.clear()
        progress.pop("diff_base_iid", None)
        progress["status"] = "done"

    except Exception as exc:
        progress["status"] = "error"
        progress["error"] = str(exc)
    finally:
        if cleanup_fn:
            try:
                cleanup_fn()
            except Exception:
                pass


# ── DB helpers ─────────────────────────────────────────────────────────────

def _get_rows(result) -> list:
    if isinstance(result, list):
        if result and isinstance(result[0], dict) and "result" in result[0]:
            return result[0].get("result") or []
        return result
    return []


def _fetch_all_ingestions() -> list[dict]:
    async def _q():
        async with get_db_client() as db:
            return await get_all_ingestions(db)
    try:
        return asyncio.run(_q())
    except Exception:
        return []


def _check_existing_ingestions(repo_path: str) -> list[dict]:
    async def _q():
        async with get_db_client() as db:
            return await get_ingestions_for_repo(db, repo_path)
    try:
        return asyncio.run(_q())
    except Exception:
        return []


# ── Graph data fetch ────────────────────────────────────────────────────────

def _fetch_graph_data(ingestion_id: str | None = None) -> tuple:
    async def _query():
        async with get_db_client() as db:
            if ingestion_id:
                p = {"iid": ingestion_id}
                repos   = await db.query("SELECT id, name FROM repo WHERE ingestion_id = $iid LIMIT 100", p)
                folders = await db.query("SELECT id, path FROM folder WHERE ingestion_id = $iid LIMIT 5000", p)
                files   = await db.query("SELECT id, path, diff_status FROM file WHERE ingestion_id = $iid LIMIT 5000", p)
                fns     = await db.query("SELECT id, name FROM `function` WHERE ingestion_id = $iid LIMIT 5000", p)
                classes = await db.query("SELECT id, name FROM `class` WHERE ingestion_id = $iid LIMIT 5000", p)
            else:
                repos   = await db.query("SELECT id, name FROM repo LIMIT 100")
                folders = await db.query("SELECT id, path FROM folder LIMIT 5000")
                files   = await db.query("SELECT id, path, diff_status FROM file LIMIT 5000")
                fns     = await db.query("SELECT id, name FROM `function` LIMIT 5000")
                classes = await db.query("SELECT id, name FROM `class` LIMIT 5000")

            contains_edges = await db.query("SELECT in, out FROM contains LIMIT 5000")
            folder_edges   = await db.query("SELECT in, out FROM in_folder LIMIT 5000")
            repo_edges     = await db.query("SELECT in, out FROM in_repo LIMIT 5000")

        return repos, folders, files, fns, classes, contains_edges, folder_edges, repo_edges

    return asyncio.run(_query())


def _build_agraph(
    repos, folders, files, fns, classes,
    contains_raw, folder_edges_raw, repo_edges_raw,
    diff_highlights: dict | None = None,
) -> tuple[list, list]:
    nodes: list[Node] = []
    edges: list[Edge] = []
    seen: set[str] = set()

    for row in _get_rows(repos):
        nid = str(row.get("id", ""))
        label = str(row.get("name", ""))
        if nid and nid not in seen:
            nodes.append(Node(id=nid, label=label, color=REPO_COLOR, size=35))
            seen.add(nid)

    for row in _get_rows(folders):
        nid = str(row.get("id", ""))
        label = str(row.get("path", "")).split("/")[-1] or str(row.get("path", ""))
        if nid and nid not in seen:
            nodes.append(Node(id=nid, label=label, color=FOLDER_COLOR, size=25))
            seen.add(nid)

    for row in _get_rows(files):
        nid = str(row.get("id", ""))
        label = Path(str(row.get("path", ""))).name
        ds = row.get("diff_status")
        color = DIFF_COLORS[ds] if ds in DIFF_COLORS else FILE_COLOR
        if nid and nid not in seen:
            nodes.append(Node(id=nid, label=label, color=color, size=20))
            seen.add(nid)

    for row in _get_rows(fns):
        nid = str(row.get("id", ""))
        label = str(row.get("name", ""))
        if nid and nid not in seen:
            nodes.append(Node(id=nid, label=label, color=FUNC_COLOR, size=12))
            seen.add(nid)

    for row in _get_rows(classes):
        nid = str(row.get("id", ""))
        label = str(row.get("name", ""))
        if nid and nid not in seen:
            nodes.append(Node(id=nid, label=label, color=CLASS_COLOR, size=15))
            seen.add(nid)

    for row in _get_rows(contains_raw):
        src = str(row.get("in", ""))
        dst = str(row.get("out", ""))
        if src and dst and src in seen and dst in seen:
            edges.append(Edge(source=src, target=dst))

    for row in _get_rows(folder_edges_raw):
        src = str(row.get("in", ""))
        dst = str(row.get("out", ""))
        if src and dst and src in seen and dst in seen:
            edges.append(Edge(source=src, target=dst))

    for row in _get_rows(repo_edges_raw):
        src = str(row.get("in", ""))
        dst = str(row.get("out", ""))
        if src and dst and src in seen and dst in seen:
            edges.append(Edge(source=src, target=dst))

    return nodes, edges


# ── Context graph helpers ──────────────────────────────────────────────────

def _parse_result_block(block: str) -> dict:
    data = {}
    for line in block.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            data[key.strip()] = val.strip()
    return data


def _extract_context_refs(messages: list) -> list[dict]:
    for msg in reversed(messages):
        if getattr(msg, "name", None) == "hybrid_search":
            content = msg.content
            if isinstance(content, str):
                try:
                    items = json.loads(content)
                except Exception:
                    items = [content]
            else:
                items = content
            refs = []
            for block in items:
                if isinstance(block, str) and block.startswith("function:"):
                    refs.append(_parse_result_block(block))
            return refs
    return []


async def _build_context_graph_async(refs: list[dict], one_hop: bool) -> tuple[list, list, str]:
    names = list({r["function"] for r in refs if r.get("function")})
    file_paths = list({r["file"] for r in refs if r.get("file")})
    raw_class_strs = [r["class"] for r in refs if r.get("class")]
    class_names = list({s.split("(")[0].strip() for s in raw_class_strs})

    if not names or not file_paths:
        return [], [], ""

    async with get_db_client() as db:
        fn_rows = _get_rows(await db.query(
            "SELECT id, name, class_name, file.id AS file_id, file.path AS file_path "
            "FROM `function` WHERE name IN $names AND file.path IN $file_paths",
            {"names": names, "file_paths": file_paths},
        ))
        file_rows = _get_rows(await db.query(
            "SELECT id, path FROM file WHERE path IN $file_paths",
            {"file_paths": file_paths},
        ))
        class_rows = []
        if class_names:
            class_rows = _get_rows(await db.query(
                "SELECT id, name FROM `class` WHERE name IN $class_names AND file.path IN $file_paths",
                {"class_names": class_names, "file_paths": file_paths},
            ))
        contains_rows = _get_rows(await db.query(
            "SELECT in, out FROM contains WHERE in.path IN $file_paths",
            {"file_paths": file_paths},
        )) if file_paths else []

        hop_fn_rows: list[dict] = []
        hop_class_rows: list[dict] = []
        if one_hop and file_paths:
            hop_fn_rows = _get_rows(await db.query(
                "SELECT id, name, class_name FROM `function` WHERE file.path IN $file_paths",
                {"file_paths": file_paths},
            ))
            hop_class_rows = _get_rows(await db.query(
                "SELECT id, name FROM `class` WHERE file.path IN $file_paths",
                {"file_paths": file_paths},
            ))

    nodes: list[Node] = []
    edges: list[Edge] = []
    seen: set[str] = set()

    for row in file_rows:
        nid = str(row.get("id", ""))
        label = Path(str(row.get("path", ""))).name
        if nid and nid not in seen:
            nodes.append(Node(id=nid, label=label, color=FILE_COLOR, size=20))
            seen.add(nid)

    for row in fn_rows:
        nid = str(row.get("id", ""))
        label = str(row.get("name", ""))
        if nid and nid not in seen:
            nodes.append(Node(id=nid, label=label, color=FUNC_COLOR, size=12,
                              font={"color": "#FFD700"}))
            seen.add(nid)

    for row in class_rows:
        nid = str(row.get("id", ""))
        label = str(row.get("name", ""))
        if nid and nid not in seen:
            nodes.append(Node(id=nid, label=label, color=CLASS_COLOR, size=15,
                              font={"color": "#FFD700"}))
            seen.add(nid)

    if one_hop:
        for row in hop_fn_rows:
            nid = str(row.get("id", ""))
            label = str(row.get("name", ""))
            if nid and nid not in seen:
                nodes.append(Node(id=nid, label=label, color=FUNC_COLOR, size=12))
                seen.add(nid)
        for row in hop_class_rows:
            nid = str(row.get("id", ""))
            label = str(row.get("name", ""))
            if nid and nid not in seen:
                nodes.append(Node(id=nid, label=label, color=CLASS_COLOR, size=15))
                seen.add(nid)

    for row in contains_rows:
        src = str(row.get("in", ""))
        dst = str(row.get("out", ""))
        if src and dst and src in seen and dst in seen:
            edges.append(Edge(source=src, target=dst))

    names_list = ", ".join(f"'{n}'" for n in names)
    paths_list = ", ".join(f"'{p}'" for p in file_paths)
    query_str = (
        f"SELECT id, name, class_name\n"
        f"FROM `function`\n"
        f"WHERE name IN [{names_list}]\n"
        f"  AND file.path IN [{paths_list}];"
    )
    if one_hop:
        query_str += (
            "\n\n-- one-hop: all nodes in same files\n"
            f"SELECT id, name FROM `function`\n"
            f"WHERE file.path IN [{paths_list}];\n"
            f"SELECT id, name FROM `class`\n"
            f"WHERE file.path IN [{paths_list}];"
        )

    return nodes, edges, query_str


def _build_context_graph(refs: list[dict], one_hop: bool) -> tuple[list, list, str]:
    return asyncio.run(_build_context_graph_async(refs, one_hop))


# ── Session-state initialisation ───────────────────────────────────────────

def _init_state() -> None:
    defaults: dict = {
        "session_id": str(uuid.uuid4())[:8],
        "messages": [],
        "ingest_thread": None,
        "ingest_stop_event": None,
        "ingest_thread_id": None,
        "ingest_progress": {"processed": 0, "total": 0, "status": "idle"},
        "diff_highlights": {},
        "diff_status_log": [],
        "pending_ingest": None,
        "selected_repo_path": None,
        "selected_ingestion_id": None,
        "context_refs": [],
        "show_one_hop": False,
        "ctx_graph_nodes": [],
        "ctx_graph_edges": [],
        "ctx_query": "",
        "graph_interval": 60,
        "graph_last_refreshed": "",
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


# ── Ingestion control helpers ──────────────────────────────────────────────

def _start_ingestion(pending: dict, prev_ingestion_id: str | None) -> None:
    """Kick off the background ingestion thread."""
    repo_path = pending["repo_path"]
    disk_path = pending.get("disk_path") or repo_path
    github_url = pending.get("github_url")
    cleanup_fn = pending.get("cleanup_fn")

    # Unique thread ID per run so resume logic is scoped correctly
    thread_id = (
        f"ingest-{hashlib.md5(repo_path.encode()).hexdigest()[:8]}"
        f"-{uuid.uuid4().hex[:4]}"
    )

    stop_event = threading.Event()
    diff_highlights: dict = {}
    diff_status_log: list = []
    new_progress: dict = {
        "processed": 0, "total": 0, "status": "running",
        "diff_base_iid": prev_ingestion_id,
    }

    st.session_state.ingest_thread_id = thread_id
    st.session_state.ingest_stop_event = stop_event
    st.session_state.ingest_progress = new_progress
    st.session_state.diff_highlights = diff_highlights
    st.session_state.diff_status_log = diff_status_log

    t = threading.Thread(
        target=_run_ingestion,
        args=(
            repo_path, disk_path, github_url, prev_ingestion_id,
            thread_id, stop_event, new_progress,
            diff_highlights, diff_status_log, cleanup_fn,
        ),
        daemon=True,
    )
    st.session_state.ingest_thread = t
    t.start()


def _delete_and_start(pending: dict, del_ingestion_id: str) -> None:
    """Delete an existing ingestion then start a fresh one."""
    async def _del():
        async with get_db_client() as db:
            await delete_ingestion(db, del_ingestion_id)

    asyncio.run(_del())
    _start_ingestion(pending, prev_ingestion_id=None)


# ── App ────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Dead Reckoning", layout="wide")
_init_state()


# ── Conflict dialog ─────────────────────────────────────────────────────────

@st.dialog("Repo already ingested")
def _conflict_dialog() -> None:
    pending = st.session_state.pending_ingest
    if not pending:
        st.rerun()
        return

    existing = pending["existing"]
    repo_display = pending.get("github_url") or pending["repo_path"]
    st.write(f"**{os.path.basename(repo_display.rstrip('/')) or repo_display}** "
             f"has {len(existing)} existing version(s).")

    latest = existing[0]
    at = str(latest.get("ingested_at", ""))[:19].replace("T", " ")
    fc = latest.get("file_count", "?")
    st.caption(f"Latest: {at}  —  {fc} files")

    st.divider()
    col1, col2, col3 = st.columns(3)

    with col1:
        if st.button("Add new version", use_container_width=True, type="primary"):
            _start_ingestion(pending, prev_ingestion_id=str(latest["id"]))
            st.session_state.pending_ingest = None
            st.rerun()

    with col2:
        if st.button("Replace latest", use_container_width=True):
            _delete_and_start(pending, del_ingestion_id=str(latest["id"]))
            st.session_state.pending_ingest = None
            st.rerun()

    with col3:
        if st.button("Abort", use_container_width=True):
            if pending.get("cleanup_fn"):
                pending["cleanup_fn"]()
            st.session_state.pending_ingest = None
            st.rerun()


# Show dialog if pending
if st.session_state.pending_ingest:
    _conflict_dialog()


# ── Sidebar ─────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("Dead Reckoning")
    st.caption("Navigate any codebase.")
    st.divider()

    ingest_source = st.text_input(
        "Repo path or GitHub URL",
        value="",
        key="ingest_source_input",
        placeholder="/path/to/repo  or  https://github.com/…",
    )

    thread: threading.Thread | None = st.session_state.ingest_thread
    is_alive = thread is not None and thread.is_alive()

    col1, col2 = st.columns(2)
    with col1:
        ingest_clicked = st.button(
            "Ingest", disabled=is_alive, use_container_width=True, type="primary"
        )
    with col2:
        interrupt_clicked = st.button(
            "Interrupt", disabled=not is_alive, use_container_width=True
        )

    # ── Ingest button logic ─────────────────────────────────────────────
    if ingest_clicked:
        source = ingest_source.strip()
        if not source:
            st.error("Please enter a repo path or GitHub URL.")
        else:
            if is_github_url(source):
                try:
                    disk_path, cleanup_fn = clone_repo(source)
                    repo_path = source
                    github_url = source
                except RuntimeError as e:
                    st.error(str(e))
                    disk_path = None
            else:
                repo_path = source
                disk_path = source
                github_url = None
                cleanup_fn = None

            if disk_path:
                existing = _check_existing_ingestions(repo_path)
                pending = {
                    "repo_path": repo_path,
                    "disk_path": disk_path,
                    "github_url": github_url,
                    "cleanup_fn": cleanup_fn,
                    "existing": existing,
                }
                if existing:
                    st.session_state.pending_ingest = pending
                    st.rerun()
                else:
                    _start_ingestion(pending, prev_ingestion_id=None)
                    st.rerun()

    if interrupt_clicked:
        if st.session_state.ingest_stop_event:
            st.session_state.ingest_stop_event.set()
            st.session_state.ingest_progress["status"] = "stopping"
        st.rerun()

    # ── Repo / version selector ─────────────────────────────────────────
    st.divider()
    all_ingestions = _fetch_all_ingestions()

    if all_ingestions:
        # Group by repo_path
        repos_map: dict[str, list[dict]] = {}
        for ing in all_ingestions:
            rp = ing.get("repo_path", "")
            repos_map.setdefault(rp, []).append(ing)

        repo_options = list(repos_map.keys())

        def _repo_label(rp: str) -> str:
            return os.path.basename(rp.rstrip("/")) or rp

        selected_repo = st.selectbox(
            "Repo",
            options=repo_options,
            format_func=_repo_label,
            key="repo_selector",
        )

        versions = repos_map.get(selected_repo, [])

        def _version_label(v: dict) -> str:
            at = str(v.get("ingested_at", ""))[:19].replace("T", " ")
            fc = v.get("file_count", "?")
            return f"{at}  ({fc} files)"

        selected_version = st.selectbox(
            "Version",
            options=versions,
            format_func=_version_label,
            key="version_selector",
        )

        if selected_version:
            iid = str(selected_version["id"])
            if st.session_state.selected_ingestion_id != iid:
                st.session_state.selected_ingestion_id = iid
                st.session_state.selected_repo_path = selected_repo
    else:
        st.caption("No ingestions yet.")

    # ── Status display ──────────────────────────────────────────────────
    st.divider()
    p = st.session_state.ingest_progress
    processed = p.get("processed", 0)
    total = p.get("total", 0)
    status = p.get("status", "idle")

    if is_alive and status not in ("running", "stopping"):
        status = "running"

    if status == "running":
        label = f"Indexing… {processed} / {total or '?'} files"
        st.info(label)
        if total > 0:
            st.progress(processed / total)
    elif status == "stopping":
        st.warning(f"Stopping… {processed} / {total}")
        if total > 0:
            st.progress(processed / total)
    elif status == "done":
        st.success(f"Done — {processed} / {total} files indexed.")
    elif status == "stopped":
        st.warning(f"Interrupted at {processed} / {total} files.")
    elif status == "error":
        st.error(f"Error: {p.get('error', 'unknown')}")
    else:
        st.caption("Ready. Enter a repo path and click **Ingest**.")

    st.divider()
    st.caption("🔴 repo  🟠 folder  🔵 file  🟣 function  🟢 class")
    if st.session_state.diff_highlights:
        st.caption("🟢 unchanged  🟡 modified  🔴 deleted")


# ── Main tabs ───────────────────────────────────────────────────────────────

tab_graph, tab_chat = st.tabs(["Knowledge Graph", "Ask the Codebase"])


# ── Graph refresh helper ────────────────────────────────────────────────────

def _do_graph_refresh():
    thread = st.session_state.get("ingest_thread")
    _alive = thread is not None and thread.is_alive()
    p = st.session_state.ingest_progress

    # While re-ingesting with a diff base, pin the graph to the prev version
    # so the diff_status colours on those nodes are visible.
    diff_base_iid = p.get("diff_base_iid") if _alive else None
    iid = diff_base_iid or st.session_state.get("selected_ingestion_id") or None

    try:
        data = _fetch_graph_data(iid)
        nodes, edges = _build_agraph(*data)
        st.session_state["graph_nodes"] = nodes
        st.session_state["graph_edges"] = edges
        st.session_state["graph_last_refreshed"] = datetime.now().strftime("%H:%M:%S")
        st.session_state.pop("graph_error", None)
    except Exception as exc:
        st.session_state["graph_error"] = str(exc)
        st.session_state.pop("graph_nodes", None)


# ── Tab 1: Knowledge Graph ──────────────────────────────────────────────────

with tab_graph:
    st.number_input(
        "Auto-refresh every (seconds)",
        min_value=1, max_value=600, step=5,
        key="graph_interval",
    )

    is_active = is_alive or bool(st.session_state.diff_highlights)
    run_every = 1 if is_active else st.session_state.graph_interval

    @st.fragment(run_every=run_every)
    def _graph_fragment():
        graph_col, status_col = st.columns([7, 3])

        with graph_col:
            btn_col, ts_col = st.columns([1, 3])
            with btn_col:
                st.button("Refresh now", type="primary")
            with ts_col:
                if st.session_state.get("graph_last_refreshed"):
                    st.caption(f"Last refreshed: {st.session_state['graph_last_refreshed']}")

            _do_graph_refresh()

            if "graph_error" in st.session_state:
                st.error(f"Could not load graph: {st.session_state['graph_error']}")
            elif "graph_nodes" in st.session_state:
                g_nodes = st.session_state["graph_nodes"]
                g_edges = st.session_state["graph_edges"]
                if g_nodes:
                    st.caption(f"{len(g_nodes)} nodes · {len(g_edges)} edges")
                    cfg = Config(
                        width="100%", height=540,
                        directed=True, physics=True, hierarchical=False,
                    )
                    agraph(nodes=g_nodes, edges=g_edges, config=cfg)
                else:
                    st.info("No nodes yet — select a version or run ingestion.")

        with status_col:
            st.subheader("Ingestion Status")

            _thread: threading.Thread | None = st.session_state.get("ingest_thread")
            _alive = _thread is not None and _thread.is_alive()
            p = st.session_state.ingest_progress
            _status = p.get("status", "idle")
            if _alive and _status not in ("running", "stopping"):
                _status = "running"

            if _status == "running":
                _processed = p.get("processed", 0)
                _total = p.get("total", 0)
                if _total:
                    st.progress(_processed / _total, text=f"{_processed}/{_total} files")
                else:
                    st.write("Starting…")
            elif _status == "done":
                st.success("Ingestion complete.")
            elif _status == "error":
                st.error(p.get("error", "Unknown error"))

            log = list(st.session_state.diff_status_log)
            if log:
                highlights = st.session_state.diff_highlights
                if highlights:
                    n_green  = sum(1 for v in highlights.values() if v == "green")
                    n_yellow = sum(1 for v in highlights.values() if v == "yellow")
                    n_red    = sum(1 for v in highlights.values() if v == "red")
                    st.caption(
                        f"Diff: {n_green} unchanged · {n_yellow} modified · {n_red} deleted"
                    )

                with st.container(height=440):
                    for line in reversed(log[-200:]):
                        st.caption(line)

            # When the background thread finishes, trigger a full page rerun so
            # the sidebar status and version selector both update.
            # Use a flag to fire exactly once per completion event.
            if _alive:
                st.session_state["_ingestion_was_running"] = True
            elif st.session_state.pop("_ingestion_was_running", False):
                st.rerun(scope="app")

    _graph_fragment()


# ── Tab 2: Ask the Codebase ─────────────────────────────────────────────────

with tab_chat:
    chat_col, graph_col = st.columns([3, 2])

    with chat_col:
        repo = st.session_state.get("ingest_source_input", "")
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(_linkify_paths(msg["content"], repo))

        if prompt := st.chat_input("Ask anything about the codebase…"):
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.chat_message("assistant"):
                try:
                    if "query_agent" not in st.session_state:
                        st.session_state.query_agent = build_query_agent()
                    agent = st.session_state.query_agent
                    config = {"configurable": {"thread_id": f"query-{st.session_state.session_id}"}}
                    inputs = {
                        "messages": [("user", prompt)],
                        "repo_path": st.session_state.get("ingest_source_input", ""),
                    }

                    status_widget = st.status("Thinking…", expanded=True)
                    response_area = st.empty()
                    response = ""

                    for chunk, _meta in agent.stream(inputs, config, stream_mode="messages"):
                        if getattr(chunk, "tool_call_chunks", []):
                            for tc in chunk.tool_call_chunks:
                                if tc.get("name"):
                                    with status_widget:
                                        st.write(f"Calling `{tc['name']}`…")
                        elif isinstance(chunk, ToolMessage):
                            with status_widget:
                                st.write(f"Got results from `{chunk.name}`")
                        elif (
                            isinstance(getattr(chunk, "content", None), str)
                            and chunk.content
                            and not getattr(chunk, "tool_call_chunks", [])
                        ):
                            response += chunk.content
                            response_area.markdown(response + "▌")

                    status_widget.update(label="Done", state="complete", expanded=False)
                    response_area.markdown(_linkify_paths(response, repo))

                    state = agent.get_state(config)
                    st.session_state.context_refs = _extract_context_refs(
                        state.values.get("messages", [])
                    )
                except Exception as exc:
                    response = f"Error: {exc}"
                    st.markdown(response)

            st.session_state.messages.append({"role": "assistant", "content": response})

    with graph_col:
        st.subheader("Context Graph")
        if st.session_state.context_refs:
            one_hop = st.toggle(
                "Show one hop",
                value=st.session_state.show_one_hop,
                key="one_hop_toggle",
            )
            st.session_state.show_one_hop = one_hop

            try:
                nodes, edges, query_str = _build_context_graph(
                    st.session_state.context_refs, one_hop
                )
                st.session_state.ctx_graph_nodes = nodes
                st.session_state.ctx_graph_edges = edges
                st.session_state.ctx_query = query_str

                if nodes:
                    cfg = Config(
                        width="100%", height=450,
                        directed=True, physics=True, hierarchical=False,
                    )
                    agraph(nodes=nodes, edges=edges, config=cfg)
                else:
                    st.info("No graph nodes found for this result.")
            except Exception as exc:
                st.error(f"Graph error: {exc}")

            with st.expander("SurrealDB query"):
                st.code(st.session_state.ctx_query or "", language="sql")
        else:
            st.info("Ask a question to see the context graph.")
