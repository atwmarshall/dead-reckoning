import asyncio
import os

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_ollama import OllamaEmbeddings
from surrealdb import AsyncSurreal

load_dotenv()


async def _query(sql: str, vars: dict | None = None):
    db = AsyncSurreal(os.environ["SURREALDB_URL"])
    await db.connect()
    await db.signin({"username": os.environ["SURREALDB_USER"], "password": os.environ["SURREALDB_PASS"]})
    await db.use(os.environ["SURREALDB_NS"], os.environ["SURREALDB_DB"])
    try:
        return await db.query(sql, vars or {})
    finally:
        await db.close()


@tool
def get_dependencies(module: str) -> list[str]:
    """Return the file paths that a given file imports. Pass a partial filename e.g. '_client'."""
    rows = asyncio.run(_query(
        "SELECT ->imports->file.path AS deps FROM file WHERE path CONTAINS $module",
        {"module": module},
    ))
    if not rows:
        return []
    paths = []
    for row in rows:
        for p in (row.get("deps") or []):
            if p:
                paths.append(p)
    return paths


@tool
def find_callers(function_name: str) -> list[str]:
    """Return names of functions that call the given function. Returns empty list if call edges not yet indexed."""
    rows = asyncio.run(_query(
        "SELECT <-calls<-`function`.name AS callers FROM `function` WHERE name = $name",
        {"name": function_name},
    ))
    if not rows:
        return []
    names = []
    for row in rows:
        for n in (row.get("callers") or []):
            if n:
                names.append(n)
    return names


@tool
def explain_module(module: str) -> str:
    """Return a summary of all functions and their docstrings in a given file. Pass a partial filename e.g. '_auth'."""
    rows = asyncio.run(_query(
        "SELECT name, docstring FROM `function` WHERE file.path CONTAINS $module",
        {"module": module},
    ))
    if not rows:
        return f"No functions found for module matching '{module}'."
    lines = []
    for row in rows:
        name = row.get("name", "?")
        doc = row.get("docstring") or "(no docstring)"
        lines.append(f"  {name}: {doc}")
    return f"Functions in '{module}':\n" + "\n".join(lines)


@tool
def semantic_search(query: str) -> list[str]:
    """Semantic search over function docstrings using vector similarity. Returns matching function names and docstrings."""
    embeddings = OllamaEmbeddings(
        model=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    )
    vec = embeddings.embed_query(query)

    rows = asyncio.run(_query(
        """SELECT name, docstring,
           vector::similarity::cosine(embedding, $vec) AS score
           FROM `function`
           WHERE embedding IS NOT NONE
           ORDER BY score DESC
           LIMIT 5""",
        {"vec": vec},
    ))
    if not rows:
        return ["No embedded functions found — run DEV-10 to add embeddings."]
    return [
        f"{r.get('name')}: {r.get('docstring') or '(no docstring)'}"
        for r in rows
    ]
