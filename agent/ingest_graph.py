import asyncio
import os
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph_checkpoint_surrealdb import SurrealSaver
from typing_extensions import TypedDict

from agent.graph import _ensure_checkpoint_tables
from ingestion.loader import get_db_client, load_calls, load_file
from ingestion.parser import parse_file, parse_repo

load_dotenv()


class IngestionState(TypedDict):
    messages: Annotated[list, add_messages]
    repo_path: str
    all_files: list[str]
    processed_files: list[str]
    current_file: str


def _initialize(state: IngestionState) -> dict:
    """Populate all_files from the repo on first run. No-op on resume."""
    if state.get("all_files"):
        return {}
    parsed = parse_repo(state["repo_path"])
    all_files = [p["path"] for p in parsed]
    print(f"Found {len(all_files)} files to ingest.")
    return {"all_files": all_files, "processed_files": [], "current_file": ""}


def _process_file(state: IngestionState) -> dict:
    """Parse and load the next unprocessed file into SurrealDB."""
    processed = set(state.get("processed_files") or [])
    remaining = [f for f in state["all_files"] if f not in processed]
    if not remaining:
        return {}

    current = remaining[0]
    parsed = parse_file(current)

    async def _load():
        async with get_db_client() as db:
            await load_file(parsed, db, repo_path=state["repo_path"])

    asyncio.run(_load())

    new_processed = list(state.get("processed_files") or []) + [current]
    total = len(state["all_files"])
    print(f"[{len(new_processed)}/{total}] processed: {Path(current).name}")

    return {"processed_files": new_processed, "current_file": current}


def _has_more(state: IngestionState) -> str:
    processed = set(state.get("processed_files") or [])
    remaining = [f for f in (state.get("all_files") or []) if f not in processed]
    return "process_file" if remaining else "create_call_edges"


def _create_call_edges(state: IngestionState) -> dict:
    """Second-pass node: create calls edges after all files are ingested."""
    parsed_files = parse_repo(state["repo_path"])

    async def _load():
        async with get_db_client() as db:
            return await load_calls(parsed_files, db)

    count = asyncio.run(_load())
    print(f"Call edges created: {count}")
    return {}


def build_ingestion_agent():
    asyncio.run(_ensure_checkpoint_tables())

    checkpointer = SurrealSaver(
        url=os.environ["SURREALDB_URL"],
        namespace=os.environ["SURREALDB_NS"],
        database=os.environ["SURREALDB_DB"],
        user=os.environ["SURREALDB_USER"],
        password=os.environ["SURREALDB_PASS"],
    )
    checkpointer.setup()

    graph = StateGraph(IngestionState)
    graph.add_node("initialize", _initialize)
    graph.add_node("process_file", _process_file)
    graph.add_node("create_call_edges", _create_call_edges)
    graph.set_entry_point("initialize")
    graph.add_conditional_edges("initialize", _has_more, {"process_file": "process_file", "create_call_edges": "create_call_edges"})
    graph.add_conditional_edges("process_file", _has_more, {"process_file": "process_file", "create_call_edges": "create_call_edges"})
    graph.add_edge("create_call_edges", END)

    return graph.compile(checkpointer=checkpointer)
