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
from langgraph.types import Command
from ingestion.diff import DiffEngine
from ingestion.github import clone_repo, is_github_url
from ingestion.snapshot import create_snapshot
from ingestion.loader import (
    create_ingestion,
    delete_ingestion,
    get_all_ingestions,
    get_db_client,
    get_ingestions_for_repo,
    load_file,
)

# ── Colour-blind-safe palette ─────────────────────────────────────────────
REPO_COLOR   = "#FF6D00"   # orange-red
FOLDER_COLOR = "#FFB300"   # amber
FILE_COLOR   = "#42A5F5"   # blue
FUNC_COLOR   = "#AB47BC"   # purple
CLASS_COLOR  = "#00BFA5"   # teal
CALLS_EDGE_COLOR = "#FF6D00"

DIFF_COLORS = {
    "green":  "#42A5F5",   # blue (unchanged)
    "yellow": "#FFB300",   # amber (modified)
    "red":    "#FF6D00",   # orange-red (deleted)
}

# ── Dark theme CSS ────────────────────────────────────────────────────────
CUSTOM_CSS = """
<style>
/* Sidebar compact styling */
section[data-testid="stSidebar"] .block-container { padding-top: 1rem; }
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] .stCaption,
section[data-testid="stSidebar"] .stMarkdown { font-size: 0.85rem; }
section[data-testid="stSidebar"] h4 { font-size: 1rem; margin-bottom: 0.25rem; }
section[data-testid="stSidebar"] .stExpander { border: 1px solid #333; border-radius: 6px; margin-bottom: 0.5rem; }

/* Accent colours */
.stButton > button[kind="primary"] { background-color: #00BFA5; border-color: #00BFA5; }
.stButton > button[kind="primary"]:hover { background-color: #00997F; border-color: #00997F; }

/* Node detail panel in sidebar — compact */
.sidebar-detail-header { margin-bottom: 0.25rem; font-size: 0.95rem; }
.sidebar-detail-prop { font-size: 0.82rem; margin-bottom: 0.1rem; }
</style>
"""

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
    *,
    existing_ingestion_id: str | None = None,
    resume_mode: bool = False,
) -> None:
    try:
        # 1. Create or reuse ingestion record
        if existing_ingestion_id:
            ingestion_id = existing_ingestion_id
        else:
            async def _create():
                async with get_db_client() as db:
                    return await create_ingestion(db, repo_path, github_url)

            ingestion_id = asyncio.run(_create())

        progress["ingestion_id"] = ingestion_id

        # 1.5. Create tar snapshot of current disk state (non-fatal)
        new_snapshot_path = None
        if not existing_ingestion_id:
            iid_bare = ingestion_id.split(":", 1)[1] if ":" in ingestion_id else ingestion_id
            try:
                new_snapshot_path = create_snapshot(disk_path, iid_bare)
            except Exception as snap_err:
                print(f"Snapshot creation failed (non-fatal): {snap_err}")

        # 2. Run diff if we have a previous version (skip on resume)
        if prev_ingestion_id and not resume_mode:
            diff_status_log.append("Computing diff…")

            async def _run_diff():
                async with get_db_client() as db:
                    async for event in DiffEngine.run(
                        disk_path, prev_ingestion_id, db, new_snapshot_path=new_snapshot_path
                    ):
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

        if resume_mode:
            # Resume from checkpoint — skip init, just send Command(resume=True)
            stream_cmd = Command(resume=True)
        else:
            init_state = {
                "repo_path": repo_path,
                "disk_path": disk_path,
                "ingestion_id": ingestion_id,
                "prev_ingestion_id": prev_ingestion_id or "",
                "all_files": [],
                "processed_files": [],
                "current_file": "",
            }

            for chunk in agent.stream(init_state, config, stream_mode="values"):
                pass  # initialize runs; review_diff interrupts if prev_ingestion_id

            # 3b. If interrupted (prev_ingestion_id was set), wait for user to confirm
            graph_state = agent.get_state(config)
            if graph_state.next:
                progress["status"] = "awaiting_resume"
                resume_event = progress.get("resume_event")
                resume_event.wait()  # block thread until UI signals Continue
                if stop_event.is_set():
                    progress["status"] = "stopped"
                    return
                progress["status"] = "running"
                stream_cmd = Command(resume=True)
            else:
                stream_cmd = None  # fresh ingest already finished in one stream

        # 3c. Process files
        def _update_progress(chunk):
            processed = chunk.get("processed_files") or []
            all_files = chunk.get("all_files") or []
            progress["processed"] = len(processed)
            progress["total"] = len(all_files)

            current = chunk.get("current_file", "")
            if current:
                if prev_ingestion_id:
                    prev_node_id = (
                        "file:"
                        + hashlib.md5((current + prev_ingestion_id).encode()).hexdigest()[:12]
                    )
                    diff_highlights.pop(prev_node_id, None)
                diff_status_log.append(f"Ingested: {Path(current).name}")

        if stream_cmd is not None:
            for chunk in agent.stream(stream_cmd, config, stream_mode="values"):
                _update_progress(chunk)
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
            imports_edges  = await db.query("SELECT in, out FROM imports LIMIT 5000")
            calls_edges    = await db.query("SELECT in, out FROM calls LIMIT 5000")
            inherits_edges = await db.query("SELECT in, out FROM inherits LIMIT 5000")

        return (repos, folders, files, fns, classes,
                contains_edges, folder_edges, repo_edges,
                imports_edges, calls_edges, inherits_edges)

    return asyncio.run(_query())


def _build_agraph(
    repos, folders, files, fns, classes,
    contains_raw, folder_edges_raw, repo_edges_raw,
    imports_raw=None, calls_raw=None, inherits_raw=None,
    diff_highlights: dict | None = None,
    *,
    show_repos: bool = True,
    show_folders: bool = True,
    show_files: bool = True,
    show_functions: bool = True,
    show_classes: bool = True,
    show_structural: bool = True,
    show_calls: bool = True,
) -> tuple[list, list]:
    nodes: list[Node] = []
    edges: list[Edge] = []
    seen: set[str] = set()

    if show_repos:
        for row in _get_rows(repos):
            nid = str(row.get("id", ""))
            label = str(row.get("name", ""))
            if nid and nid not in seen:
                nodes.append(Node(id=nid, label=label, color=REPO_COLOR, size=35))
                seen.add(nid)

    if show_folders:
        for row in _get_rows(folders):
            nid = str(row.get("id", ""))
            label = str(row.get("path", "")).split("/")[-1] or str(row.get("path", ""))
            if nid and nid not in seen:
                nodes.append(Node(id=nid, label=label, color=FOLDER_COLOR, size=25))
                seen.add(nid)

    if show_files:
        for row in _get_rows(files):
            nid = str(row.get("id", ""))
            label = Path(str(row.get("path", ""))).name
            ds = row.get("diff_status")
            color = DIFF_COLORS[ds] if ds in DIFF_COLORS else FILE_COLOR
            if nid and nid not in seen:
                nodes.append(Node(id=nid, label=label, color=color, size=20))
                seen.add(nid)

    if show_functions:
        for row in _get_rows(fns):
            nid = str(row.get("id", ""))
            label = str(row.get("name", ""))
            if nid and nid not in seen:
                nodes.append(Node(id=nid, label=label, color=FUNC_COLOR, size=12))
                seen.add(nid)

    if show_classes:
        for row in _get_rows(classes):
            nid = str(row.get("id", ""))
            label = str(row.get("name", ""))
            if nid and nid not in seen:
                nodes.append(Node(id=nid, label=label, color=CLASS_COLOR, size=15))
                seen.add(nid)

    structural_sources = [
        (contains_raw, "contains"),
        (folder_edges_raw, "in_folder"),
        (repo_edges_raw, "in_repo"),
    ]
    call_sources = [
        (imports_raw, "imports"),
        (calls_raw, "calls"),
        (inherits_raw, "inherits"),
    ]

    if show_structural:
        for raw, label in structural_sources:
            if raw is None:
                continue
            for row in _get_rows(raw):
                src = str(row.get("in", ""))
                dst = str(row.get("out", ""))
                if src and dst and src in seen and dst in seen:
                    edges.append(Edge(source=src, target=dst, label=label))

    if show_calls:
        for raw, label in call_sources:
            if raw is None:
                continue
            for row in _get_rows(raw):
                src = str(row.get("in", ""))
                dst = str(row.get("out", ""))
                if src and dst and src in seen and dst in seen:
                    color = CALLS_EDGE_COLOR if label == "calls" else None
                    edges.append(Edge(source=src, target=dst, label=label, color=color))

    return nodes, edges


# ── Node detail helpers ───────────────────────────────────────────────────

EDGE_TABLES = ["contains", "in_folder", "in_repo", "imports", "calls", "inherits"]

NODE_TYPE_COLORS = {
    "repo": REPO_COLOR, "folder": FOLDER_COLOR, "file": FILE_COLOR,
    "function": FUNC_COLOR, "class": CLASS_COLOR,
}


def _fetch_node_detail(node_id: str) -> dict:
    """Fetch properties, edge counts, and neighbors for a node."""
    table = node_id.split(":")[0] if ":" in node_id else "unknown"

    async def _query():
        async with get_db_client() as db:
            props_raw = await db.query(f"SELECT * FROM {node_id}")
            props = {}
            rows = _get_rows(props_raw)
            if rows:
                props = dict(rows[0])

            edge_counts = {}
            neighbors = {}
            for et in EDGE_TABLES:
                in_count_raw = await db.query(
                    f"SELECT count() AS cnt FROM {et} WHERE out = {node_id} GROUP ALL"
                )
                in_cnt = 0
                in_rows = _get_rows(in_count_raw)
                if in_rows:
                    in_cnt = in_rows[0].get("cnt", 0)

                out_count_raw = await db.query(
                    f"SELECT count() AS cnt FROM {et} WHERE in = {node_id} GROUP ALL"
                )
                out_cnt = 0
                out_rows = _get_rows(out_count_raw)
                if out_rows:
                    out_cnt = out_rows[0].get("cnt", 0)

                if in_cnt or out_cnt:
                    edge_counts[et] = {"incoming": in_cnt, "outgoing": out_cnt}

                et_neighbors = []
                if out_cnt:
                    out_neighbors_raw = await db.query(
                        f"SELECT out AS target FROM {et} WHERE in = {node_id} LIMIT 20"
                    )
                    for r in _get_rows(out_neighbors_raw):
                        tid = str(r.get("target", ""))
                        if tid:
                            et_neighbors.append({"id": tid, "direction": "outgoing"})

                if in_cnt:
                    in_neighbors_raw = await db.query(
                        f"SELECT in AS source FROM {et} WHERE out = {node_id} LIMIT 20"
                    )
                    for r in _get_rows(in_neighbors_raw):
                        sid = str(r.get("source", ""))
                        if sid:
                            et_neighbors.append({"id": sid, "direction": "incoming"})

                if et_neighbors:
                    neighbors[et] = et_neighbors

        return {"id": node_id, "table": table, "properties": props,
                "edge_counts": edge_counts, "neighbors": neighbors}

    return asyncio.run(_query())


def _render_detail_panel_sidebar(detail: dict) -> None:
    """Render node detail panel in the sidebar (compact vertical layout)."""
    table = detail["table"]
    props = detail["properties"]
    color = NODE_TYPE_COLORS.get(table, "#888")

    display_name = props.get("name") or props.get("path") or detail["id"]
    st.markdown(
        f'<h4 class="sidebar-detail-header" style="color:{color}; margin-bottom:0">'
        f'{table.upper()}: {display_name}</h4>',
        unsafe_allow_html=True,
    )

    if st.button("Close", key="close_detail"):
        st.session_state["selected_node_id"] = None
        st.session_state["selected_node_detail"] = None
        st.rerun(scope="app")

    # Properties
    st.caption("**Properties**")
    skip_keys = {"embedding", "id"}
    for k, v in props.items():
        if k in skip_keys:
            continue
        v_str = str(v)
        if len(v_str) > 150:
            v_str = v_str[:150] + "…"
        st.markdown(f'<p class="sidebar-detail-prop"><b>{k}:</b> {v_str}</p>', unsafe_allow_html=True)

    # Relationships
    edge_counts = detail.get("edge_counts", {})
    if edge_counts:
        st.caption("**Relationships**")
        for et, counts in edge_counts.items():
            st.caption(f"{et}: {counts['outgoing']} out, {counts['incoming']} in")

    # Neighbors (limited)
    neighbors = detail.get("neighbors", {})
    if neighbors:
        st.caption("**Connected Nodes**")
        for et, neighbor_list in neighbors.items():
            for nb in neighbor_list[:10]:
                nb_id = nb["id"]
                nb_table = nb_id.split(":")[0] if ":" in nb_id else "?"
                arrow = "→" if nb["direction"] == "outgoing" else "←"
                btn_label = f"{arrow} [{nb_table}] {nb_id}"
                if st.button(
                    btn_label,
                    key=f"nav_{et}_{nb['direction']}_{nb_id}",
                    help=f"Navigate to {nb_id}",
                ):
                    st.session_state["selected_node_id"] = nb_id
                    st.session_state["selected_node_detail"] = None
                    st.rerun(scope="app")


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
        # Resume state
        "ingest_ingestion_id": None,
        "ingest_repo_path": None,
        "ingest_disk_path": None,
        "ingest_github_url": None,
        "ingest_prev_ingestion_id": None,
        # Graph filter defaults
        "gf_repos": True,
        "gf_folders": True,
        "gf_files": True,
        "gf_functions": True,
        "gf_classes": True,
        "gf_structural": True,
        "gf_calls": True,
        "graph_frozen": False,
        "context_refs": [],
        "show_one_hop": False,
        "ctx_graph_nodes": [],
        "ctx_graph_edges": [],
        "ctx_query": "",
        "graph_interval": 60,
        "graph_last_refreshed": "",
        "selected_node_id": None,
        "selected_node_detail": None,
        "ingest_preset_disk_path": None,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


# ── Ingestion control helpers ──────────────────────────────────────────────

def _start_ingestion(pending: dict, prev_ingestion_id: str | None) -> None:
    """Kick off the background ingestion thread (fresh run)."""
    repo_path = pending["repo_path"]
    disk_path = pending.get("disk_path") or repo_path
    github_url = pending.get("github_url")
    cleanup_fn = pending.get("cleanup_fn")

    thread_id = (
        f"ingest-{hashlib.md5(repo_path.encode()).hexdigest()[:8]}"
        f"-{uuid.uuid4().hex[:4]}"
    )

    stop_event = threading.Event()
    resume_event = threading.Event()
    diff_highlights: dict = {}
    diff_status_log: list = []
    new_progress: dict = {
        "processed": 0, "total": 0, "status": "running",
        "diff_base_iid": prev_ingestion_id,
        "resume_event": resume_event,
    }

    # Persist context for resume
    st.session_state.ingest_thread_id = thread_id
    st.session_state.ingest_stop_event = stop_event
    st.session_state.ingest_progress = new_progress
    st.session_state.diff_highlights = diff_highlights
    st.session_state.diff_status_log = diff_status_log
    st.session_state.ingest_ingestion_id = None  # will be set by thread via progress dict
    st.session_state.ingest_repo_path = repo_path
    st.session_state.ingest_disk_path = disk_path
    st.session_state.ingest_github_url = github_url
    st.session_state.ingest_prev_ingestion_id = prev_ingestion_id

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


def _resume_ingestion() -> None:
    """Resume a stopped ingestion from its LangGraph checkpoint."""
    thread_id = st.session_state.ingest_thread_id
    ingestion_id = st.session_state.ingest_ingestion_id
    repo_path = st.session_state.ingest_repo_path
    disk_path = st.session_state.ingest_disk_path
    github_url = st.session_state.ingest_github_url
    prev_ingestion_id = st.session_state.ingest_prev_ingestion_id

    if not thread_id or not ingestion_id or not repo_path:
        return

    stop_event = threading.Event()
    resume_event = threading.Event()

    # Preserve existing diff state
    diff_highlights = st.session_state.diff_highlights
    diff_status_log = st.session_state.diff_status_log

    p = st.session_state.ingest_progress
    new_progress: dict = {
        "processed": p.get("processed", 0),
        "total": p.get("total", 0),
        "status": "running",
        "diff_base_iid": prev_ingestion_id,
        "resume_event": resume_event,
        "ingestion_id": ingestion_id,
    }

    st.session_state.ingest_stop_event = stop_event
    st.session_state.ingest_progress = new_progress

    t = threading.Thread(
        target=_run_ingestion,
        args=(
            repo_path, disk_path, github_url, prev_ingestion_id,
            thread_id, stop_event, new_progress,
            diff_highlights, diff_status_log, None,
        ),
        kwargs={
            "existing_ingestion_id": ingestion_id,
            "resume_mode": True,
        },
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


def _get_ingest_status() -> str:
    """Get the reconciled ingestion status, handling edge cases."""
    thread: threading.Thread | None = st.session_state.ingest_thread
    is_alive = thread is not None and thread.is_alive()
    p = st.session_state.ingest_progress
    status = p.get("status", "idle")

    # Sync ingestion_id from progress dict (background thread sets it)
    prog_iid = p.get("ingestion_id")
    if prog_iid and st.session_state.ingest_ingestion_id != prog_iid:
        st.session_state.ingest_ingestion_id = prog_iid

    # Status reconciliation
    if status == "stopping" and not is_alive:
        status = "stopped"
        p["status"] = "stopped"
    elif is_alive and status not in ("running", "stopping", "awaiting_resume"):
        status = "running"

    return status


# ── App ────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Dead Reckoning", layout="wide")
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)
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

status = _get_ingest_status()

with st.sidebar:
    st.title("Dead Reckoning")
    st.caption("Navigate any codebase.")

    # ── Expander 1: Ingestion ──────────────────────────────────────────
    ingest_expanded = status not in ("done",)
    with st.expander("Ingestion", expanded=ingest_expanded):
        _FIXTURE_ROOT = Path(__file__).parent.parent / "tests/fixtures/sample_repo"
        _PRESET_REPOS = [
            ("v1 — sample repo", str(_FIXTURE_ROOT), str(_FIXTURE_ROOT / "v1")),
            ("v2 — sample repo (with changes)", str(_FIXTURE_ROOT), str(_FIXTURE_ROOT / "v2")),
        ]
        with st.expander("Quick select", expanded=False):
            for _label, _repo_path, _disk_path in _PRESET_REPOS:
                if st.button(_label, use_container_width=True, key=f"preset_{_label}"):
                    st.session_state.ingest_source_input = _repo_path
                    st.session_state.ingest_preset_disk_path = _disk_path
                    st.rerun()

        ingest_source = st.text_input(
            "Repo path or GitHub URL",
            value="",
            key="ingest_source_input",
            placeholder="/path/to/repo  or  https://github.com/…",
        )

        # ── Single stateful button ─────────────────────────────────────
        if status in ("idle", "done", "error"):
            btn_clicked = st.button("Ingest", use_container_width=True, type="primary")
            if btn_clicked:
                source = ingest_source.strip()
                if not source:
                    st.error("Please enter a repo path or GitHub URL.")
                else:
                    disk_path = None
                    if is_github_url(source):
                        try:
                            disk_path, cleanup_fn = clone_repo(source)
                            repo_path = source
                            github_url = source
                        except RuntimeError as e:
                            st.error(str(e))
                    else:
                        repo_path = source
                        disk_path = source
                        github_url = None
                        cleanup_fn = None

                    # Preset may supply a separate disk_path (e.g. fixture v1/v2)
                    preset_disk = st.session_state.pop("ingest_preset_disk_path", None)
                    if preset_disk and not is_github_url(source):
                        disk_path = preset_disk

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
                            # Clear old resume state for fresh ingest
                            st.session_state.ingest_ingestion_id = None
                            _start_ingestion(pending, prev_ingestion_id=None)
                            st.rerun()

        elif status in ("running", "awaiting_resume"):
            if st.button("Pause", use_container_width=True):
                if st.session_state.ingest_stop_event:
                    st.session_state.ingest_stop_event.set()
                p = st.session_state.ingest_progress
                resume_ev = p.get("resume_event")
                if resume_ev:
                    resume_ev.set()  # unblock thread if waiting at diff review
                p["status"] = "stopping"
                st.rerun()

        elif status == "stopped":
            if st.button("Resume", use_container_width=True, type="primary"):
                _resume_ingestion()
                st.rerun()

        elif status == "stopping":
            st.button("Stopping…", use_container_width=True, disabled=True)

        # ── Progress display ───────────────────────────────────────────
        p = st.session_state.ingest_progress
        processed = p.get("processed", 0)
        total = p.get("total", 0)

        if status == "running":
            label = f"Indexing… {processed} / {total or '?'} files"
            st.info(label)
            if total > 0:
                st.progress(processed / total)
        elif status == "awaiting_resume":
            st.info("Diff ready — review the graph, then click Resume.")
        elif status == "stopping":
            st.warning(f"Stopping… {processed} / {total}")
            if total > 0:
                st.progress(processed / total)
        elif status == "done":
            st.success(f"Done — {processed} / {total} files indexed.")
        elif status == "stopped":
            st.warning(f"Paused at {processed} / {total} files.")
        elif status == "error":
            st.error(f"Error: {p.get('error', 'unknown')}")
        else:
            st.caption("Ready. Enter a repo path and click **Ingest**.")

        # ── Diff log ───────────────────────────────────────────────────
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
            with st.container(height=200):
                for line in reversed(log[-100:]):
                    st.caption(line)

    # ── Expander 2: Node Details ───────────────────────────────────────
    selected = st.session_state.get("selected_node_id")
    with st.expander("Node Details", expanded=bool(selected)):
        if selected:
            detail = st.session_state.get("selected_node_detail")
            if detail is None or detail.get("id") != selected:
                try:
                    detail = _fetch_node_detail(selected)
                    st.session_state["selected_node_detail"] = detail
                except Exception as exc:
                    st.error(f"Could not load node: {exc}")
                    detail = None
            if detail:
                _render_detail_panel_sidebar(detail)
        else:
            st.caption("Click a node in the graph to see details.")

    # ── Expander 3: Settings & Legend ──────────────────────────────────
    with st.expander("Settings & Legend", expanded=False):
        # Repo / version selector
        all_ingestions = _fetch_all_ingestions()

        if all_ingestions:
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

            # Build version options with "All versions" at top
            all_versions_sentinel = {"id": "__all__", "_label": "All versions"}
            version_options = [all_versions_sentinel] + versions

            def _version_label(v: dict) -> str:
                if v.get("id") == "__all__":
                    return "All versions"
                at = str(v.get("ingested_at", ""))[:19].replace("T", " ")
                fc = v.get("file_count", "?")
                snap = v.get("snapshot_path")
                snap_info = ""
                if snap:
                    snap_file = Path(snap)
                    if snap_file.exists():
                        size_kb = snap_file.stat().st_size // 1024
                        snap_info = f" · {size_kb}KB tar"
                return f"{at}  ({fc} files{snap_info})"

            selected_version = st.selectbox(
                "Version",
                options=version_options,
                format_func=_version_label,
                key="version_selector",
            )

            if selected_version:
                if selected_version.get("id") == "__all__":
                    if st.session_state.selected_ingestion_id is not None:
                        st.session_state.selected_ingestion_id = None
                        st.session_state.selected_repo_path = selected_repo
                    st.caption("Showing all versions")
                else:
                    iid = str(selected_version["id"])
                    if st.session_state.selected_ingestion_id != iid:
                        st.session_state.selected_ingestion_id = iid
                        st.session_state.selected_repo_path = selected_repo
                    # Show thread_id as caption
                    thread_id = st.session_state.get("ingest_thread_id", "")
                    if thread_id:
                        st.caption(f"Thread: `{thread_id}`")
        else:
            st.caption("No ingestions yet.")

        st.divider()

        # Auto-refresh
        st.number_input(
            "Auto-refresh (seconds)",
            min_value=1, max_value=600, step=5,
            key="graph_interval",
        )

        st.divider()

        # Graph filters
        st.caption("**Node filters**")
        st.session_state.gf_repos      = st.checkbox("Repos",      value=st.session_state.gf_repos)
        st.session_state.gf_folders    = st.checkbox("Folders",    value=st.session_state.gf_folders)
        st.session_state.gf_files      = st.checkbox("Files",      value=st.session_state.gf_files)
        st.session_state.gf_functions  = st.checkbox("Functions",  value=st.session_state.gf_functions)
        st.session_state.gf_classes    = st.checkbox("Classes",    value=st.session_state.gf_classes)
        st.caption("**Edge filters**")
        st.session_state.gf_structural = st.checkbox("Structural (contains / folder / repo)", value=st.session_state.gf_structural)
        st.session_state.gf_calls      = st.checkbox("Calls",      value=st.session_state.gf_calls)

        st.divider()

        # Legend
        st.caption("**Node types**")
        st.markdown(
            f'<span style="color:{REPO_COLOR}">&#9679;</span> Repo &nbsp; '
            f'<span style="color:{FOLDER_COLOR}">&#9679;</span> Folder &nbsp; '
            f'<span style="color:{FILE_COLOR}">&#9679;</span> File &nbsp; '
            f'<span style="color:{FUNC_COLOR}">&#9679;</span> Function &nbsp; '
            f'<span style="color:{CLASS_COLOR}">&#9679;</span> Class',
            unsafe_allow_html=True,
        )
        if st.session_state.diff_highlights:
            st.caption("**Diff**")
            st.markdown(
                f'<span style="color:{DIFF_COLORS["green"]}">&#9679;</span> Unchanged &nbsp; '
                f'<span style="color:{DIFF_COLORS["yellow"]}">&#9679;</span> Modified &nbsp; '
                f'<span style="color:{DIFF_COLORS["red"]}">&#9679;</span> Deleted',
                unsafe_allow_html=True,
            )


# ── Main tabs ───────────────────────────────────────────────────────────────

tab_graph, tab_chat = st.tabs(["Knowledge Graph", "Ask the Codebase"])


# ── Graph refresh helper ────────────────────────────────────────────────────

def _do_graph_refresh():
    thread = st.session_state.get("ingest_thread")
    _alive = thread is not None and thread.is_alive()
    p = st.session_state.ingest_progress

    # While re-ingesting with a diff base, pin the graph to the prev version
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


# ── Tab 1: Knowledge Graph (full-width) ─────────────────────────────────────

with tab_graph:
    thread: threading.Thread | None = st.session_state.ingest_thread
    is_alive = thread is not None and thread.is_alive()
    is_active = is_alive or bool(st.session_state.diff_highlights)
    run_every = 1 if is_active else st.session_state.graph_interval

    @st.fragment(run_every=run_every)
    def _graph_fragment():
        btn_col, ts_col = st.columns([1, 5])
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
                    width="100%", height=700,
                    directed=True, physics=True, hierarchical=False,
                )
                clicked_node = agraph(nodes=g_nodes, edges=g_edges, config=cfg)
                if clicked_node and clicked_node != st.session_state.get("selected_node_id"):
                    st.session_state["selected_node_id"] = clicked_node
                    st.session_state["selected_node_detail"] = None
                    st.rerun(scope="app")
            else:
                st.info("No nodes yet — select a version or run ingestion.")

        # When the background thread finishes, trigger a full page rerun
        _thread: threading.Thread | None = st.session_state.get("ingest_thread")
        _alive = _thread is not None and _thread.is_alive()
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
