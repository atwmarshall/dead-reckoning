"""Demo seed script — wipes all data, applies schema, ingests /tmp/demo-repo."""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from surrealdb import AsyncSurreal

from ingestion.loader import get_db_client, load_file
from ingestion.parser import parse_repo

load_dotenv()

SCHEMA_PATH = Path(__file__).parent.parent / "ingestion" / "schema.surql"
DEMO_REPO = "/tmp/demo-repo"

# All tables to wipe — knowledge graph + LangGraph checkpoint tables
ALL_TABLES = ["repo", "folder", "file", "`function`", "`class`", "contains", "imports", "calls", "inherits", "in_folder", "in_repo", "checkpoint", "`write`"]


async def reset_database() -> None:
    """Wipe all data from all tables and re-apply schema for a clean slate."""
    url = os.environ["SURREALDB_URL"]
    user = os.environ["SURREALDB_USER"]
    password = os.environ["SURREALDB_PASS"]
    ns = os.environ["SURREALDB_NS"]
    db_name = os.environ["SURREALDB_DB"]

    db = AsyncSurreal(url)
    await db.connect()
    await db.signin({"username": user, "password": password})
    await db.use(ns, db_name)

    for table in ALL_TABLES:
        await db.query(f"DELETE {table}")
    print(f"Wiped {len(ALL_TABLES)} tables.")

    schema = SCHEMA_PATH.read_text()
    for stmt in schema.split(";"):
        stmt = stmt.strip()
        if stmt and not stmt.startswith("--"):
            await db.query(stmt)
    print("Schema applied.")

    await db.close()


async def verify_counts() -> dict:
    """Query DB for final node counts."""
    async with get_db_client() as db:
        file_res = await db.query("SELECT count() FROM file GROUP ALL")
        fn_res = await db.query("SELECT count() FROM `function` GROUP ALL")
        cls_res = await db.query("SELECT count() FROM `class` GROUP ALL")

    def _count(res):
        try:
            return res[0]["count"]
        except (IndexError, KeyError, TypeError):
            return 0

    return {
        "files": _count(file_res),
        "functions": _count(fn_res),
        "classes": _count(cls_res),
    }


async def ingest(repo_path: str) -> None:
    """Parse and load all files from the demo repo."""
    files = parse_repo(repo_path)
    total = len(files)
    print(f"Ingesting {total} files from {repo_path} ...")

    async with get_db_client() as db:
        for i, parsed in enumerate(files, 1):
            short = parsed["path"].replace(repo_path, "").lstrip("/")
            print(f"  [{i}/{total}] {short}")
            await load_file(parsed, db, repo_path=repo_path)


async def main() -> None:
    print("=== Demo seed starting ===\n")

    print("Step 1/3: Resetting database ...")
    await reset_database()

    print("\nStep 2/3: Ingesting demo repo ...")
    await ingest(DEMO_REPO)

    print("\nStep 3/3: Verifying counts ...")
    counts = await verify_counts()

    print(
        f"\nDemo ready. "
        f"Files: {counts['files']} | "
        f"Functions: {counts['functions']} | "
        f"Classes: {counts['classes']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
