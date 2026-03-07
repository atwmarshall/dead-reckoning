import asyncio
import json
import re
import threading
import uuid
from pathlib import Path

import streamlit as st
from streamlit_agraph import Config, Edge, Node, agraph

from agent.graph import build_query_agent
from agent.ingest_graph import build_ingestion_agent
from ingestion.loader import get_db_client

# ── Colours ────────────────────────────────────────────────────────────────
FOLDER_COLOR = "#F39C12"  # orange
FILE_COLOR = "#4C8BF5"   # blue
FUNC_COLOR = "#9B59B6"   # purple
CLASS_COLOR = "#27AE60"  # green

# Matches file paths like /abs/path/file.py:42 or rel/path/file.py:42
# Requires at least one slash so dotted names (agent.graph) are excluded.
_PATH_RE = re.compile(
    r"`((?:/[\w./-]+\.\w{1,6})(?::\d+)?)`"          # `backtick-wrapped`
    r"|(?<![\[(`/])((?:/[\w./-]+\.\w{1,6})(?::\d+)?)"  # /absolute/path.ext
    r"|(?<![\[(`/\w])((?:[\w.-]+/)+[\w.-]+\.\w{1,6}(?::\d+)?)"  # relative/path.ext
)


def _linkify_paths(text: str, repo_path: str = "") -> str:
    """Replace file paths in text with vscode:// clickable markdown links."""
    def _make_link(raw: str) -> str:
        path_part, _, line_part = raw.partition(":")
        if path_part.startswith("/"):
            abs_path = path_part
        elif repo_path:
            abs_path = f"{repo_path.rstrip('/')}/{path_part}"
        else:
            return f"`{raw}`"
        url = f"vscode://file{abs_path}" + (f":{line_part}" if line_part else "")
        return f"[`{raw}`]({url})"

    def _replace(m: re.Match) -> str:
        raw = m.group(1) or m.group(2) or m.group(3)
        return _make_link(raw)

    return _PATH_RE.sub(_replace, text)


# ── Background ingestion thread ────────────────────────────────────────────

def _run_ingestion(
    repo_path: str,
    thread_id: str,
    stop_event: threading.Event,
    progress: dict,
    is_resume: bool,
) -> None:
    """Run the LangGraph ingestion agent in a background thread.

    Streams one step at a time so we can honour a clean-stop request between
    files.  The checkpoint is written by LangGraph after every step, so
    stopping here always leaves the DB in a consistent, resumable state.
    """
    agent = build_ingestion_agent()
    config = {"configurable": {"thread_id": thread_id}}

    init_state = (
        None
        if is_resume
        else {
            "repo_path": repo_path,
            "all_files": [],
            "processed_files": [],
            "current_file": "",
        }
    )

    try:
        for chunk in agent.stream(init_state, config, stream_mode="values"):
            processed = len(chunk.get("processed_files") or [])
            total = len(chunk.get("all_files") or [])
            progress["processed"] = processed
            progress["total"] = total

            # Clean stop: check after current file finishes (checkpoint saved).
            if stop_event.is_set():
                progress["status"] = "stopped"
                return

        progress["status"] = "done"
    except Exception as exc:
        progress["status"] = "error"
        progress["error"] = str(exc)


# ── SurrealDB graph fetch ──────────────────────────────────────────────────

def _fetch_graph_data() -> tuple:
    async def _query():
        async with get_db_client() as db:
            folders = await db.query("SELECT id, path FROM folder LIMIT 5000")
            files = await db.query("SELECT id, path FROM file LIMIT 5000")
            fns = await db.query("SELECT id, name FROM `function` LIMIT 5000")
            classes = await db.query("SELECT id, name FROM `class` LIMIT 5000")
            contains_edges = await db.query("SELECT in, out FROM contains LIMIT 5000")
            folder_edges = await db.query("SELECT in, out FROM in_folder LIMIT 5000")
        return folders, files, fns, classes, contains_edges, folder_edges

    return asyncio.run(_query())


def _get_rows(result) -> list:
    """Unwrap SurrealDB query response (list-of-result-dicts or plain list)."""
    if isinstance(result, list):
        if result and isinstance(result[0], dict) and "result" in result[0]:
            return result[0].get("result") or []
        return result
    return []


def _build_agraph(folders, files, fns, classes, contains_raw, folder_edges_raw) -> tuple[list, list]:
    nodes: list[Node] = []
    edges: list[Edge] = []
    seen: set[str] = set()

    for row in _get_rows(folders):
        nid = str(row.get("id", ""))
        label = str(row.get("path", "")).split("/")[-1] or str(row.get("path", ""))
        if nid and nid not in seen:
            nodes.append(Node(id=nid, label=label, color=FOLDER_COLOR, size=25))
            seen.add(nid)

    for row in _get_rows(files):
        nid = str(row.get("id", ""))
        label = Path(str(row.get("path", ""))).name
        if nid and nid not in seen:
            nodes.append(Node(id=nid, label=label, color=FILE_COLOR, size=20))
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
    """Pull parsed node dicts from the last hybrid_search ToolMessage."""
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

        file_ids = [str(r["id"]) for r in file_rows if r.get("id")]

        contains_rows = _get_rows(await db.query(
            "SELECT in, out FROM contains WHERE in IN $file_ids",
            {"file_ids": file_ids},
        )) if file_ids else []

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
            nodes.append(Node(
                id=nid, label=label,
                color={"background": FUNC_COLOR, "border": "#FFD700"},
                size=14, borderWidth=3,
            ))
            seen.add(nid)

    for row in class_rows:
        nid = str(row.get("id", ""))
        label = str(row.get("name", ""))
        if nid and nid not in seen:
            nodes.append(Node(
                id=nid, label=label,
                color={"background": CLASS_COLOR, "border": "#FFD700"},
                size=16, borderWidth=3,
            ))
            seen.add(nid)

    if one_hop:
        for row in hop_fn_rows:
            nid = str(row.get("id", ""))
            label = str(row.get("name", ""))
            if nid and nid not in seen:
                nodes.append(Node(id=nid, label=label, color=FUNC_COLOR, size=10))
                seen.add(nid)

        for row in hop_class_rows:
            nid = str(row.get("id", ""))
            label = str(row.get("name", ""))
            if nid and nid not in seen:
                nodes.append(Node(id=nid, label=label, color=CLASS_COLOR, size=11))
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
        "context_refs": [],
        "show_one_hop": False,
        "ctx_graph_nodes": [],
        "ctx_graph_edges": [],
        "ctx_query": "",
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


# ── App ────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Dead Reckoning", layout="wide")
_init_state()

# ── Sidebar ────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("Dead Reckoning")
    st.caption("Navigate any codebase.")
    st.divider()

    repo_path = st.text_input("Repo path", value="/tmp/demo-repo", key="repo_path_input")

    # Derive live running state from thread liveness
    thread: threading.Thread | None = st.session_state.ingest_thread
    is_alive = thread is not None and thread.is_alive()

    col1, col2 = st.columns(2)

    with col1:
        ingest_clicked = st.button(
            "Ingest",
            disabled=is_alive,
            use_container_width=True,
            type="primary",
        )

    with col2:
        interrupt_clicked = st.button(
            "Interrupt",
            disabled=not is_alive,
            use_container_width=True,
        )

    if ingest_clicked:
        thread_id = f"ingest-{Path(repo_path).name}"
        is_resume = st.session_state.ingest_thread_id == thread_id

        stop_event = threading.Event()
        prev = st.session_state.ingest_progress
        new_progress: dict = {
            # Preserve prior count when resuming so the display doesn't reset to 0
            "processed": prev.get("processed", 0) if is_resume else 0,
            "total": prev.get("total", 0) if is_resume else 0,
            "status": "running",
        }

        st.session_state.ingest_thread_id = thread_id
        st.session_state.ingest_stop_event = stop_event
        st.session_state.ingest_progress = new_progress

        t = threading.Thread(
            target=_run_ingestion,
            args=(repo_path, thread_id, stop_event, new_progress, is_resume),
            daemon=True,
        )
        st.session_state.ingest_thread = t
        t.start()
        st.rerun()

    if interrupt_clicked:
        if st.session_state.ingest_stop_event:
            st.session_state.ingest_stop_event.set()
            st.session_state.ingest_progress["status"] = "stopping"
        st.rerun()

    # ── Status display ─────────────────────────────────────────────────────
    p = st.session_state.ingest_progress
    processed = p.get("processed", 0)
    total = p.get("total", 0)
    status = p.get("status", "idle")

    # If thread is alive but status hasn't been set to running yet, show running
    if is_alive and status not in ("running", "stopping"):
        status = "running"

    st.divider()
    if status == "running":
        label = f"Indexing… {processed} / {total or '?'} files"
        st.info(label)
        if total > 0:
            st.progress(processed / total)
    elif status == "stopping":
        st.warning(f"Stopping after current file… {processed} / {total}")
        if total > 0:
            st.progress(processed / total)
    elif status == "done":
        st.success(f"Done — {processed} / {total} files indexed.")
    elif status == "stopped":
        st.warning(f"Interrupted at {processed} / {total} files.\nClick **Ingest** to resume.")
    elif status == "error":
        st.error(f"Error: {p.get('error', 'unknown')}")
    else:
        st.caption("Ready. Enter a repo path and click **Ingest**.")

    st.divider()
    st.caption("🟠 folder  🔵 file  🟣 function  🟢 class")


# ── Main tabs ──────────────────────────────────────────────────────────────
tab_graph, tab_chat = st.tabs(["Knowledge Graph", "Ask the Codebase"])


# ── Tab 1: Knowledge Graph ─────────────────────────────────────────────────
with tab_graph:
    if st.button("Refresh graph", type="primary"):
        try:
            with st.spinner("Loading graph from SurrealDB…"):
                folders, files, fns, classes, contains_edges, folder_edges = _fetch_graph_data()
            nodes, edges = _build_agraph(folders, files, fns, classes, contains_edges, folder_edges)
            st.session_state["graph_nodes"] = nodes
            st.session_state["graph_edges"] = edges
            st.session_state.pop("graph_error", None)
        except Exception as exc:
            st.session_state["graph_error"] = str(exc)
            st.session_state.pop("graph_nodes", None)
        st.rerun()

    if "graph_error" in st.session_state:
        st.error(f"Could not load graph: {st.session_state['graph_error']}")
    elif "graph_nodes" in st.session_state:
        g_nodes: list = st.session_state["graph_nodes"]
        g_edges: list = st.session_state["graph_edges"]
        if g_nodes:
            st.caption(f"{len(g_nodes)} nodes · {len(g_edges)} edges")
            cfg = Config(
                width="100%",
                height=620,
                directed=True,
                physics=True,
                hierarchical=False,
            )
            agraph(nodes=g_nodes, edges=g_edges, config=cfg)
        else:
            st.info("No nodes in the database yet. Run ingestion first, then refresh.")
    else:
        st.info("Click **Refresh graph** to load the knowledge graph.")


# ── Tab 2: Ask the Codebase ────────────────────────────────────────────────
with tab_chat:
    chat_col, graph_col = st.columns([3, 2])

    with chat_col:
        repo = st.session_state.get("repo_path_input", "")
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(_linkify_paths(msg["content"], repo))

        if prompt := st.chat_input("Ask anything about the codebase…"):
            st.session_state.messages.append({"role": "user", "content": prompt})
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.chat_message("assistant"):
                with st.spinner("Thinking…"):
                    try:
                        if "query_agent" not in st.session_state:
                            st.session_state.query_agent = build_query_agent()

                        agent = st.session_state.query_agent
                        chat_thread_id = f"query-{st.session_state.session_id}"
                        result = agent.invoke(
                            {
                                "messages": [("user", prompt)],
                                "repo_path": st.session_state.repo_path_input,
                            },
                            {"configurable": {"thread_id": chat_thread_id}},
                        )
                        response = result["messages"][-1].content
                        st.session_state.context_refs = _extract_context_refs(result["messages"])
                    except Exception as exc:
                        response = f"Error: {exc}"

                st.markdown(_linkify_paths(response, st.session_state.get("repo_path_input", "")))

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
                        width="100%",
                        height=450,
                        directed=True,
                        physics=True,
                        hierarchical=False,
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
