import asyncio
import logging
import os
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv

logger = logging.getLogger("dead-reckoning.ingest")
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from langgraph_checkpoint_surrealdb import SurrealSaver
from typing_extensions import TypedDict

from agent.graph import _ensure_checkpoint_tables
from ingestion.diff import content_hash_file
from ingestion.enricher import enrich_functions
from ingestion.loader import finalize_ingestion, get_db_client, load_calls, load_file
from ingestion.parser import parse_file, parse_repo

load_dotenv()


class IngestionState(TypedDict):
    messages: Annotated[list, add_messages]
    repo_path: str      # canonical identifier stored in DB (URL or local path)
    disk_path: str      # actual filesystem path used for parsing (same as repo_path for local)
    ingestion_id: str
    prev_ingestion_id: str  # empty string for fresh ingests
    all_files: list[str]
    processed_files: list[str]
    current_file: str


def _initialize(state: IngestionState) -> dict:
    """Populate all_files from the repo on first run. No-op on resume."""
    if state.get("all_files"):
        logger.info("Resume: all_files already populated (%d files)", len(state["all_files"]))
        return {}
    disk = state.get("disk_path") or state["repo_path"]
    logger.info("Discovering files in %s", disk)
    parsed = parse_repo(disk)
    all_files = [p["path"] for p in parsed]
    logger.info("Found %d files to ingest", len(all_files))
    return {"all_files": all_files, "processed_files": [], "current_file": ""}


def _process_file(state: IngestionState) -> dict:
    """Parse and load the next unprocessed file into SurrealDB."""
    processed = set(state.get("processed_files") or [])
    remaining = [f for f in state["all_files"] if f not in processed]
    if not remaining:
        return {}

    current = remaining[0]
    ingestion_id = state.get("ingestion_id", "")

    parsed = parse_file(current)

    try:
        content_hash = content_hash_file(current)
    except OSError:
        content_hash = None

    disk = state.get("disk_path") or state["repo_path"]

    async def _load():
        async with get_db_client() as db:
            await load_file(
                parsed,
                db,
                repo_path=state["repo_path"],
                ingestion_id=ingestion_id,
                content_hash=content_hash,
                disk_path=disk,
            )

    logger.info("Loading %s into SurrealDB...", Path(current).name)
    asyncio.run(_load())
    logger.info("Loaded %s", Path(current).name)

    new_processed = list(state.get("processed_files") or []) + [current]
    total = len(state["all_files"])
    logger.info("[%d/%d] processed: %s", len(new_processed), total, Path(current).name)

    return {"processed_files": new_processed, "current_file": current}


def _finalize(state: IngestionState) -> dict:
    """Mark the ingestion record as done."""
    ingestion_id = state.get("ingestion_id", "")
    if not ingestion_id:
        return {}

    file_count = len(state.get("processed_files") or [])

    async def _do_finalize():
        async with get_db_client() as db:
            await finalize_ingestion(db, ingestion_id, file_count)

    asyncio.run(_do_finalize())
    logger.info("Ingestion %s finalized: %d files", ingestion_id, file_count)
    return {}


def _review_diff(state: IngestionState) -> dict:
    """Pause for user to review diff colours. No-op on fresh ingests."""
    if state.get("prev_ingestion_id"):
        interrupt("diff_review")
    return {}


def _has_more(state: IngestionState) -> str:
    processed = set(state.get("processed_files") or [])
    remaining = [f for f in (state.get("all_files") or []) if f not in processed]
    return "process_file" if remaining else "create_call_edges"


def _create_call_edges(state: IngestionState) -> dict:
    """Second-pass node: create calls edges after all files are ingested."""
    disk = state.get("disk_path") or state["repo_path"]
    parsed_files = parse_repo(disk)

    async def _load():
        async with get_db_client() as db:
            return await load_calls(parsed_files, db, ingestion_id=state.get("ingestion_id", ""))

    count = asyncio.run(_load())
    logger.info("Call edges created: %d", count)
    return {}


def _enrich_summaries(state: IngestionState) -> dict:
    """Third-pass node: generate synthetic summaries for undocumented functions."""
    async def _run():
        async with get_db_client() as db:
            return await enrich_functions(db)

    count = asyncio.run(_run())
    logger.info("Enriched %d undocumented functions", count)
    return {}


def build_ingestion_agent():
    logger.info("Building ingestion agent — ensuring checkpoint tables...")
    asyncio.run(_ensure_checkpoint_tables())
    logger.info("Checkpoint tables ready, creating SurrealSaver...")

    checkpointer = SurrealSaver(
        url=os.environ["SURREALDB_URL"],
        namespace=os.environ["SURREALDB_NS"],
        database=os.environ["SURREALDB_DB"],
        user=os.environ["SURREALDB_USER"],
        password=os.environ["SURREALDB_PASS"],
    )
    logger.info("Calling checkpointer.setup()...")
    checkpointer.setup()
    logger.info("Checkpointer ready, compiling graph...")

    graph = StateGraph(IngestionState)
    graph.add_node("initialize", _initialize)
    graph.add_node("review_diff", _review_diff)
    graph.add_node("process_file", _process_file)
    graph.add_node("create_call_edges", _create_call_edges)
    graph.add_node("enrich_summaries", _enrich_summaries)
    graph.add_node("finalize", _finalize)
    graph.set_entry_point("initialize")
    graph.add_edge("initialize", "review_diff")
    graph.add_conditional_edges(
        "review_diff", _has_more,
        {"process_file": "process_file", "create_call_edges": "create_call_edges"},
    )
    graph.add_conditional_edges(
        "process_file", _has_more,
        {"process_file": "process_file", "create_call_edges": "create_call_edges"},
    )
    graph.add_edge("create_call_edges", "enrich_summaries")
    graph.add_edge("enrich_summaries", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)
