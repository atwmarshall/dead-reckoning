import asyncio
import threading
import uuid
from pathlib import Path

import streamlit as st
from streamlit_agraph import Config, Edge, Node, agraph

from agent.graph import build_query_agent
from agent.ingest_graph import build_ingestion_agent
from ingestion.loader import get_db_client

# ── Colours ────────────────────────────────────────────────────────────────
REPO_COLOR   = "#E74C3C"  # red
FOLDER_COLOR = "#F39C12"  # orange
FILE_COLOR = "#4C8BF5"   # blue
FUNC_COLOR = "#9B59B6"   # purple
CLASS_COLOR = "#27AE60"  # green


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
            repos = await db.query("SELECT id, name FROM repo LIMIT 100")
            folders = await db.query("SELECT id, path FROM folder LIMIT 5000")
            files = await db.query("SELECT id, path FROM file LIMIT 5000")
            fns = await db.query("SELECT id, name FROM `function` LIMIT 5000")
            classes = await db.query("SELECT id, name FROM `class` LIMIT 5000")
            contains_edges = await db.query("SELECT in, out FROM contains LIMIT 5000")
            folder_edges = await db.query("SELECT in, out FROM in_folder LIMIT 5000")
            repo_edges = await db.query("SELECT in, out FROM in_repo LIMIT 5000")
        return repos, folders, files, fns, classes, contains_edges, folder_edges, repo_edges

    return asyncio.run(_query())


def _get_rows(result) -> list:
    """Unwrap SurrealDB query response (list-of-result-dicts or plain list)."""
    if isinstance(result, list):
        if result and isinstance(result[0], dict) and "result" in result[0]:
            return result[0].get("result") or []
        return result
    return []


def _build_agraph(repos, folders, files, fns, classes, contains_raw, folder_edges_raw, repo_edges_raw) -> tuple[list, list]:
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

    for row in _get_rows(repo_edges_raw):
        src = str(row.get("in", ""))
        dst = str(row.get("out", ""))
        if src and dst and src in seen and dst in seen:
            edges.append(Edge(source=src, target=dst))

    return nodes, edges


# ── Session-state initialisation ───────────────────────────────────────────

def _init_state() -> None:
    defaults: dict = {
        "session_id": str(uuid.uuid4())[:8],
        "messages": [],
        "ingest_thread": None,
        "ingest_stop_event": None,
        "ingest_thread_id": None,
        "ingest_progress": {"processed": 0, "total": 0, "status": "idle"},
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
    st.caption("🔴 repo  🟠 folder  🔵 file  🟣 function  🟢 class")


# ── Main tabs ──────────────────────────────────────────────────────────────
tab_graph, tab_chat = st.tabs(["Knowledge Graph", "Ask the Codebase"])


# ── Tab 1: Knowledge Graph ─────────────────────────────────────────────────
with tab_graph:
    if st.button("Refresh graph", type="primary"):
        try:
            with st.spinner("Loading graph from SurrealDB…"):
                repos, folders, files, fns, classes, contains_edges, folder_edges, repo_edges = _fetch_graph_data()
            nodes, edges = _build_agraph(repos, folders, files, fns, classes, contains_edges, folder_edges, repo_edges)
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
    # Render existing message history
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    if prompt := st.chat_input("Ask anything about the codebase…"):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.write(prompt)

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
                except Exception as exc:
                    response = f"Error: {exc}"

            st.write(response)

        st.session_state.messages.append({"role": "assistant", "content": response})
