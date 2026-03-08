import argparse
import asyncio

from ingestion.enricher import enrich_functions
from ingestion.loader import get_db_client, load_calls, load_file
from ingestion.parser import parse_repo


async def seed(repo_path: str, *, enrich: bool = True) -> None:
    files = parse_repo(repo_path)
    total = len(files)
    print(f"Found {total} Python files to index")

    totals = {"functions": 0, "classes": 0, "edges": 0}

    async with get_db_client() as db:
        for i, parsed in enumerate(files, 1):
            short = parsed["path"].replace(repo_path, "").lstrip("/")
            print(f"[{i}/{total}] {short}")
            counts = await load_file(parsed, db, repo_path=repo_path)
            for k in totals:
                totals[k] += counts[k]

    print("Creating call edges ...")
    async with get_db_client() as db:
        call_edges = await load_calls(files, db)

    enriched = 0
    if enrich:
        print("Enriching undocumented functions ...")
        async with get_db_client() as db:
            enriched = await enrich_functions(db)

    print(
        f"\nDone. Files processed: {total} | "
        f"Functions processed: {totals['functions']} | "
        f"Classes processed: {totals['classes']} | "
        f"Edges processed: {totals['edges']} | "
        f"Call edges: {call_edges} | "
        f"Enriched: {enriched} "
        f"(DB deduplicates by name+file — stored count will be lower)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a Python repo into SurrealDB")
    parser.add_argument("--repo", required=True, help="Path to the Python repo to index")
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip LLM enrichment of undocumented functions",
    )
    args = parser.parse_args()
    asyncio.run(seed(args.repo, enrich=not args.no_enrich))


if __name__ == "__main__":
    main()