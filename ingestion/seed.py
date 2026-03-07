import argparse
import asyncio

from ingestion.loader import get_db_client, load_file
from ingestion.parser import parse_repo


async def seed(repo_path: str) -> None:
    files = parse_repo(repo_path)
    total = len(files)
    print(f"Found {total} Python files to index")

    totals = {"functions": 0, "classes": 0, "edges": 0}

    async with get_db_client() as db:
        for i, parsed in enumerate(files, 1):
            short = parsed["path"].replace(repo_path, "").lstrip("/")
            print(f"[{i}/{total}] {short}")
            counts = await load_file(parsed, db)
            for k in totals:
                totals[k] += counts[k]

    print(
        f"\nDone. Files processed: {total} | "
        f"Functions processed: {totals['functions']} | "
        f"Classes processed: {totals['classes']} | "
        f"Edges processed: {totals['edges']} "
        f"(DB deduplicates by name+file — stored count will be lower)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a Python repo into SurrealDB")
    parser.add_argument("--repo", required=True, help="Path to the Python repo to index")
    args = parser.parse_args()
    asyncio.run(seed(args.repo))


if __name__ == "__main__":
    main()
