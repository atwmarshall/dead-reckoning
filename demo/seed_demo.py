"""Demo seed script — sets up v1 (and optionally v2 + diff) for the live demo.

Usage:
    uv run python demo/seed_demo.py               # reset + v1 fixture only
    uv run python demo/seed_demo.py --with-v2      # reset + v1 + v2 + diff (for testing)
    uv run python demo/seed_demo.py --httpx         # reset + ingest httpx (well-known real repo)
    uv run python demo/seed_demo.py --no-reset      # add v1 fixture WITHOUT resetting (use after --httpx)
    uv run python demo/seed_demo.py --reset-only    # wipe all data, apply schema
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from surrealdb import AsyncSurreal

from ingestion.diff import DiffEngine, content_hash_file
from ingestion.github import clone_repo
from ingestion.loader import (
    create_ingestion,
    finalize_ingestion,
    get_db_client,
    load_calls,
    load_file,
)
from ingestion.parser import parse_repo
from ingestion.snapshot import create_snapshot

load_dotenv()

SCHEMA_PATH = Path(__file__).parent.parent / "ingestion" / "schema.surql"
FIXTURES = Path(__file__).parent.parent / "tests" / "fixtures" / "sample_repo"
V1_PATH = FIXTURES / "v1"
V2_PATH = FIXTURES / "v2"

HTTPX_URL = "https://github.com/encode/httpx"

# All tables to wipe
ALL_TABLES = [
    "ingestion", "repo", "folder", "file", "`function`", "`class`",
    "contains", "imports", "calls", "inherits", "in_folder", "in_repo",
    "checkpoint", "`write`",
]


async def reset_database() -> None:
    """Wipe all data and re-apply schema."""
    db = AsyncSurreal(os.environ["SURREALDB_URL"])
    await db.connect()
    await db.signin({"username": os.environ["SURREALDB_USER"], "password": os.environ["SURREALDB_PASS"]})
    await db.use(os.environ["SURREALDB_NS"], os.environ["SURREALDB_DB"])

    for table in ALL_TABLES:
        await db.query(f"DELETE {table}")
    print(f"  Wiped {len(ALL_TABLES)} tables.")

    schema = SCHEMA_PATH.read_text()
    for stmt in schema.split(";"):
        stmt = stmt.strip()
        if stmt and not stmt.startswith("--"):
            try:
                await db.query(stmt)
            except Exception as e:
                # Some DEFINE statements may need the comment stripped
                lines = [l for l in stmt.splitlines() if not l.strip().startswith("--")]
                clean = "\n".join(lines).strip()
                if clean:
                    await db.query(clean)
    print("  Schema applied.")
    await db.close()


async def ingest_version(
    repo_path: Path,
    label: str,
    *,
    disk_path: Path | None = None,
) -> str:
    """Ingest a repo version with proper ingestion record + snapshot. Returns ingestion_id.

    repo_path  – canonical identifier stored in DB (used for conflict detection).
    disk_path  – actual filesystem path to parse. Defaults to repo_path.
    """
    disk = disk_path or repo_path
    repo_str = str(repo_path.resolve())
    disk_str = str(disk.resolve())
    parsed_files = parse_repo(disk_str)
    total = len(parsed_files)

    async with get_db_client() as db:
        # Create ingestion record
        ingestion_id = await create_ingestion(db, repo_str)
        iid_bare = ingestion_id.split(":", 1)[1] if ":" in ingestion_id else ingestion_id
        print(f"  Ingestion ID: {ingestion_id}")

        # Create tar snapshot
        snap = create_snapshot(disk_str, iid_bare)
        print(f"  Snapshot: {snap}")

        # Load all files
        for i, parsed in enumerate(parsed_files, 1):
            short = Path(parsed["path"]).name
            ch = None
            try:
                ch = content_hash_file(parsed["path"])
            except OSError:
                pass
            await load_file(
                parsed, db,
                repo_path=repo_str,
                ingestion_id=ingestion_id,
                content_hash=ch,
                disk_path=disk_str,
            )
            print(f"  [{i}/{total}] {short}")

        # Create call edges
        edges = await load_calls(parsed_files, db, ingestion_id=ingestion_id)
        print(f"  Edges: calls={edges['calls']}, imports={edges['imports']}")

        # Finalize
        await finalize_ingestion(db, ingestion_id, total)

    print(f"  {label} ingested: {total} files")
    return ingestion_id


async def compute_diff(repo_path: Path, prev_ingestion_id: str, new_snapshot_id: str, new_ingestion_id: str | None = None) -> int:
    """Run diff between previous ingestion and new snapshot. Returns event count."""
    from ingestion.snapshot import SNAPSHOT_DIR

    new_snap = SNAPSHOT_DIR / f"{new_snapshot_id}.tar"
    repo_str = str(repo_path.resolve())
    count = 0

    async with get_db_client() as db:
        async for event in DiffEngine.run(
            repo_str, prev_ingestion_id, db,
            new_snapshot_path=new_snap,
            new_ingestion_id=new_ingestion_id,
        ):
            status = event["status"].upper()
            name = Path(event.get("path", "?")).name if event.get("path") else event.get("name", "?")
            print(f"  {status}: {name}")
            count += 1

    return count


async def verify_counts() -> dict:
    async with get_db_client() as db:
        file_res = await db.query("SELECT count() FROM file GROUP ALL")
        fn_res = await db.query("SELECT count() FROM `function` GROUP ALL")
        cls_res = await db.query("SELECT count() FROM `class` GROUP ALL")
        diff_res = await db.query("SELECT count() FROM file WHERE diff_status IS NOT NONE GROUP ALL")

    def _count(res):
        try:
            return res[0]["count"]
        except (IndexError, KeyError, TypeError):
            return 0

    return {
        "files": _count(file_res),
        "functions": _count(fn_res),
        "classes": _count(cls_res),
        "diff_files": _count(diff_res),
    }


async def ingest_httpx() -> str:
    """Clone and ingest encode/httpx — a well-known Python HTTP client library."""
    print(f"  Cloning {HTTPX_URL} ...")
    local_path, cleanup = clone_repo(HTTPX_URL)
    try:
        ingestion_id = await ingest_version(Path(local_path), "httpx")
    finally:
        cleanup()
    return ingestion_id


async def main(args) -> None:
    print("=== Demo seed ===\n")

    # Reset unless --no-reset
    if not args.no_reset:
        print("Step 1: Resetting database...")
        await reset_database()
    else:
        print("Step 1: Skipping reset (--no-reset)")

    if args.reset_only:
        print("\nDone (reset only).")
        return

    # Ingest httpx if requested
    if args.httpx:
        print(f"\nStep 2: Ingesting httpx ({HTTPX_URL})...")
        await ingest_httpx()

        # Verify
        print("\nVerifying...")
        counts = await verify_counts()
        print(
            f"\nDemo ready (httpx).\n"
            f"  Files: {counts['files']} | "
            f"Functions: {counts['functions']} | "
            f"Classes: {counts['classes']}"
        )
        print(
            f"\nNext steps:\n"
            f"  1. uv run streamlit run ui/app.py\n"
            f"  2. Explore the httpx knowledge graph\n"
            f"  3. Ask: 'which functions handle authentication?'\n"
            f"  4. Ask: 'what would break if I changed send_request?'"
        )
        return

    # Ingest v1 — use FIXTURES as repo_path so both v1 and v2 share the same
    # canonical path, matching the UI quick-select presets.
    print(f"\nStep 2: Ingesting v1 ({V1_PATH})...")
    v1_id = await ingest_version(FIXTURES, "v1", disk_path=V1_PATH)

    # Optionally ingest v2 + diff
    if args.with_v2:
        print(f"\nStep 3: Ingesting v2 ({V2_PATH})...")
        v2_id = await ingest_version(FIXTURES, "v2", disk_path=V2_PATH)

        v2_bare = v2_id.split(":", 1)[1] if ":" in v2_id else v2_id
        print(f"\nStep 4: Computing diff (v1 → v2)...")
        diff_count = await compute_diff(V1_PATH, v1_id, v2_bare, new_ingestion_id=v2_id)
        print(f"  {diff_count} diff events")
    else:
        print("\n  (v2 skipped — ingest v2 live through the UI during the demo)")

    # Verify
    print("\nVerifying...")
    counts = await verify_counts()
    print(
        f"\nDemo ready.\n"
        f"  Files: {counts['files']} | "
        f"Functions: {counts['functions']} | "
        f"Classes: {counts['classes']} | "
        f"Diff files: {counts['diff_files']}"
    )

    if not args.with_v2:
        print(
            f"\nNext steps:\n"
            f"  1. uv run streamlit run ui/app.py\n"
            f"  2. In the UI, ingest v2 ({V2_PATH}) to trigger live diff\n"
            f"  3. Ask: 'What changed between versions? If anything new is undocumented, suggest a docstring and raise a GitHub issue.'"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed demo data for DeadReckoning")
    parser.add_argument("--with-v2", action="store_true", help="Also ingest v2 and compute diff")
    parser.add_argument("--httpx", action="store_true", help="Ingest encode/httpx (well-known real repo)")
    parser.add_argument("--no-reset", action="store_true", help="Skip DB reset (add data on top of existing)")
    parser.add_argument("--reset-only", action="store_true", help="Only wipe data and apply schema")
    args = parser.parse_args()
    asyncio.run(main(args))
