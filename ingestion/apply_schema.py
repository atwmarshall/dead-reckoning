"""Apply schema.surql to SurrealDB — no CLI required."""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from surrealdb import AsyncSurreal

load_dotenv()

SCHEMA_PATH = Path(__file__).parent / "schema.surql"


async def main() -> None:
    db = AsyncSurreal(os.environ["SURREALDB_URL"])
    await db.connect()
    await db.signin({"username": os.environ["SURREALDB_USER"], "password": os.environ["SURREALDB_PASS"]})
    await db.use(os.environ["SURREALDB_NS"], os.environ["SURREALDB_DB"])

    schema = SCHEMA_PATH.read_text()
    for stmt in schema.split(";"):
        stmt = stmt.strip()
        if stmt and not stmt.startswith("--"):
            await db.query(stmt)

    await db.close()
    print("Schema applied.")


if __name__ == "__main__":
    asyncio.run(main())
