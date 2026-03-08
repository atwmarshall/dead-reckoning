import asyncio
import os
import re

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_ollama import OllamaEmbeddings
from langsmith import traceable
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


async def _query_raw(sql: str, vars: dict | None = None):
    """Use query_raw for multi-statement queries (LET/RETURN) where query() returns None."""
    db = AsyncSurreal(os.environ["SURREALDB_URL"])
    await db.connect()
    await db.signin({"username": os.environ["SURREALDB_USER"], "password": os.environ["SURREALDB_PASS"]})
    await db.use(os.environ["SURREALDB_NS"], os.environ["SURREALDB_DB"])
    try:
        resp = await db.query_raw(sql, vars or {})
        # query_raw returns {"id": ..., "result": [{"result": ..., "status": "OK"}, ...]}
        if isinstance(resp, dict):
            stmts = resp.get("result") or []
            if isinstance(stmts, list) and stmts:
                last = stmts[-1]
                if isinstance(last, dict):
                    return last.get("result") or []
        return []
    finally:
        await db.close()


_STOP_WORDS = {
    "what", "does", "do", "the", "a", "an", "function", "method", "class",
    "tell", "me", "about", "how", "is", "are", "in", "for", "to", "of",
    "and", "or", "it", "can", "you", "give", "show", "explain", "get",
    "changed", "between", "versions", "new", "deleted", "modified",
}

_embedder = OllamaEmbeddings(
    model=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
    base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
)


def _clean(obj):
    """Strip embedding arrays and internal keys for readable LangSmith traces."""
    if isinstance(obj, list):
        return [_clean(x) for x in obj]
    if isinstance(obj, dict):
        return {
            k: _clean(v)
            for k, v in obj.items()
            if k not in ("self", "embedding", "score", "id", "rrf_score")
        }
    return obj


def _get_rows(result) -> list:
    """Extract rows from a SurrealDB query response."""
    if isinstance(result, list):
        if result and isinstance(result[0], dict) and "result" in result[0]:
            return result[0].get("result") or []
        return result
    return []


# ---------------------------------------------------------------------------
# Graph enrichment (parent class + sibling functions)
# ---------------------------------------------------------------------------

async def _enrich_one(doc: dict) -> dict:
    """Add graph context: inferred parent class and sibling function names."""
    path = doc.get("path")
    if not path and isinstance(doc.get("file"), dict):
        path = doc["file"].get("path")
    lineno = doc.get("lineno")
    name = doc.get("name")
    if not path:
        return doc

    parent_class, siblings = await asyncio.gather(
        _query(
            """SELECT name, bases, lineno FROM `class`
               WHERE file.path = $path AND lineno < $lineno
               ORDER BY lineno DESC LIMIT 1""",
            {"path": path, "lineno": lineno or 0},
        ),
        _query(
            """SELECT name FROM `function`
               WHERE file.path = $path AND class_name = $class_name AND name != $name
               LIMIT 20""",
            {"path": path, "class_name": doc.get("class_name"), "name": name},
        ),
    )

    pc_rows = _get_rows(parent_class)
    doc["_parent_class"] = pc_rows[0] if pc_rows else {}
    doc["_siblings"] = [r.get("name") for r in _get_rows(siblings) if r.get("name")]
    doc["path"] = path
    return doc


async def _enrich_all(docs: list[dict]) -> list[dict]:
    return await asyncio.gather(*[_enrich_one(doc) for doc in docs])


def _format(doc: dict) -> str:
    name = doc.get("name", "?")
    path = doc.get("path") or "?"
    docstring = (doc.get("docstring") or "").strip()
    documented = doc.get("has_docstring", bool(docstring))

    parent = doc.get("_parent_class") or {}
    siblings = doc.get("_siblings") or []

    lines = [
        f"function: {name}",
        f"file:     {path}",
        f"status:   {'documented' if documented else 'undocumented'}",
    ]
    if parent.get("name"):
        bases = ", ".join(parent.get("bases") or [])
        cls_str = parent["name"] + (f"({bases})" if bases else "")
        lines.append(f"class:    {cls_str}")
    if siblings:
        lines.append(f"siblings: {', '.join(siblings)}")
    if docstring:
        lines.append(f"summary:  {docstring}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 1: hybrid_search — SurrealDB native RRF (vector + BM25 fusion)
# ---------------------------------------------------------------------------

@traceable(name="hybrid_search", run_type="retriever", process_outputs=_clean)
def _do_hybrid_search(query: str) -> list[str]:
    vec = _embedder.embed_query(query)
    terms = [t for t in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", query)
             if t.lower() not in _STOP_WORDS and len(t) > 2]
    keyword = max(terms, key=len) if terms else query.split()[0]

    rows = asyncio.run(_query_raw(
        """
        LET $vs = SELECT *, file.path AS path,
                         vector::similarity::cosine(embedding, $vec) AS score
                  FROM `function`
                  WHERE embedding <|5,100|> $vec;
        LET $ft = SELECT *, file.path AS path,
                         search::score(0) + search::score(1) AS score
                  FROM `function`
                  WHERE name @0@ $keyword OR docstring @1@ $keyword
                  ORDER BY score DESC LIMIT 10;
        RETURN search::rrf([$vs, $ft], 5, 60);
        """,
        {"vec": vec, "keyword": keyword},
    ))

    enriched = asyncio.run(_enrich_all(rows[:5]))
    results = [_format(doc) for doc in enriched]
    if len(rows) > 5:
        results.append(
            f"note: results truncated at 5. "
            f"Refine your query or ask about a specific file to explore further."
        )
    return results


@tool
def hybrid_search(query: str) -> list[str]:
    """Search the codebase combining semantic similarity and keyword matching.
    Use for any question about what a function does, finding functions by name,
    or exploring codebase concepts."""
    return _do_hybrid_search(query)


# ---------------------------------------------------------------------------
# Tool 2: trace_impact — multi-hop graph traversal on calls edges
# ---------------------------------------------------------------------------

@tool
@traceable(name="trace_impact", run_type="retriever", process_outputs=_clean)
def trace_impact(symbol: str) -> str:
    """Find everything that calls or depends on a given function or file — direct and transitive.
    Use when asked what would break, what depends on something, or for impact analysis.
    Pass a function name or partial filename."""
    rows = asyncio.run(_query(
        """
        SELECT
            name,
            file.path AS path,
            <-calls<-`function`.name AS direct_callers,
            <-calls<-`function`.file.path AS caller_files,
            <-calls<-`function`<-calls<-`function`.name AS transitive_callers
        FROM `function`
        WHERE name CONTAINS $symbol OR file.path CONTAINS $symbol
        """,
        {"symbol": symbol},
    ))
    rows = _get_rows(rows)
    if not rows:
        return f"No functions found matching '{symbol}'."

    lines = []
    for row in rows:
        name = row.get("name", "?")
        path = row.get("path", "?")

        direct = row.get("direct_callers") or []
        if not isinstance(direct, list):
            direct = [direct]
        direct = [c for c in direct if c]

        caller_files = row.get("caller_files") or []
        if not isinstance(caller_files, list):
            caller_files = [caller_files]
        caller_files = list(dict.fromkeys(f for f in caller_files if f))

        transitive = row.get("transitive_callers") or []
        if not isinstance(transitive, list):
            transitive = [transitive]
        transitive = [t for t in transitive if t and t not in direct]

        lines.append(f"function: {name}")
        lines.append(f"file:     {path}")
        if direct:
            lines.append(f"  direct callers ({len(direct)}):     {', '.join(direct)}")
        if caller_files:
            lines.append(f"  caller files ({len(caller_files)}):      {', '.join(caller_files)}")
        if transitive:
            lines.append(f"  transitive callers ({len(transitive)}): {', '.join(transitive)}")
        if not direct and not transitive:
            lines.append("  (no known callers — leaf function)")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 3: version_diff — diff status from versioned knowledge graph
# ---------------------------------------------------------------------------

@tool
@traceable(name="version_diff", run_type="retriever", process_outputs=_clean)
def version_diff(module: str = "") -> str:
    """Summarise what changed in the most recent version comparison.
    Pass a filename to filter, or leave empty for the full diff summary.
    Use when asked what changed, what's new, or what was deleted."""
    # Auto-discover versions from ingestion table
    ingestions = _get_rows(asyncio.run(_query(
        "SELECT * FROM ingestion ORDER BY created_at DESC LIMIT 5"
    )))

    version_header = ""
    if ingestions:
        latest = ingestions[0]
        repo_name = latest.get("repo_name", latest.get("repo_path", "unknown"))
        latest_time = latest.get("created_at", "?")
        latest_files = latest.get("file_count", "?")
        if len(ingestions) >= 2:
            prev = ingestions[1]
            prev_time = prev.get("created_at", "?")
            prev_files = prev.get("file_count", "?")
            version_header = (
                f"Comparing versions for: {repo_name}\n"
                f"  Previous: {prev_time} ({prev_files} files)\n"
                f"  Current:  {latest_time} ({latest_files} files)\n\n"
            )
        else:
            version_header = (
                f"Repository: {repo_name}\n"
                f"  Latest ingestion: {latest_time} ({latest_files} files)\n"
                f"  (Only one version found — ingest again to compare)\n\n"
            )

    file_condition = "AND path CONTAINS $module" if module else ""
    file_rows = _get_rows(asyncio.run(_query(
        f"""
        SELECT path, diff_status,
               ->contains->`function`.name AS functions
        FROM file
        WHERE diff_status IS NOT NONE {file_condition}
        ORDER BY diff_status
        """,
        {"module": module} if module else {},
    )))

    fn_condition = "AND file.path CONTAINS $module" if module else ""
    fn_rows = _get_rows(asyncio.run(_query(
        f"""
        SELECT name, diff_status, file.path AS path
        FROM `function`
        WHERE diff_status IS NOT NONE {fn_condition}
        ORDER BY diff_status
        """,
        {"module": module} if module else {},
    )))

    if not file_rows and not fn_rows:
        return version_header + "No version diff data found. Ingest two versions to see what changed."

    files_by_status: dict[str, list[str]] = {"red": [], "yellow": [], "green": []}
    for row in file_rows:
        s = row.get("diff_status", "")
        if s in files_by_status:
            files_by_status[s].append(row.get("path", "?"))

    fns_by_status: dict[str, list[str]] = {"red": [], "yellow": [], "green": []}
    for row in fn_rows:
        s = row.get("diff_status", "")
        if s in fns_by_status:
            fns_by_status[s].append(f"{row.get('name', '?')} in {row.get('path', '?')}")

    lines = []
    if version_header:
        lines.append(version_header.rstrip())
    lines.extend(["Version Diff Summary", "=" * 40])

    if files_by_status["red"]:
        lines.append(f"\nDELETED files ({len(files_by_status['red'])}):")
        for p in files_by_status["red"]:
            lines.append(f"  - {p}")
    if files_by_status["yellow"]:
        lines.append(f"\nMODIFIED files ({len(files_by_status['yellow'])}):")
        for p in files_by_status["yellow"]:
            lines.append(f"  ~ {p}")
    if files_by_status["green"]:
        lines.append(f"\nUNCHANGED files ({len(files_by_status['green'])}):")
        for p in files_by_status["green"]:
            lines.append(f"  . {p}")

    if fns_by_status["red"]:
        lines.append(f"\nDELETED functions ({len(fns_by_status['red'])}):")
        for f in fns_by_status["red"]:
            lines.append(f"  - {f}")
    if fns_by_status["yellow"]:
        lines.append(f"\nMODIFIED functions ({len(fns_by_status['yellow'])}):")
        for f in fns_by_status["yellow"]:
            lines.append(f"  ~ {f}")

    total = sum(len(v) for v in files_by_status.values())
    changed = len(files_by_status["yellow"]) + len(files_by_status["red"])
    lines.append(f"\nTotal: {total} files tracked, {changed} changed or deleted")

    return "\n".join(lines)
