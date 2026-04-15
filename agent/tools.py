import asyncio
import os
import re
import subprocess

import ollama as ollama_client
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
# Graph enrichment (parent class, siblings, call neighbourhood)
# ---------------------------------------------------------------------------

_CALL_NEIGHBOUR_LIMIT = 10


async def _enrich_one(doc: dict) -> dict:
    """Add graph context: parent class, sibling functions, and call neighbourhood
    (immediate callers and callees traversed over `calls` edges)."""
    path = doc.get("path")
    if not path and isinstance(doc.get("file"), dict):
        path = doc["file"].get("path")
    lineno = doc.get("lineno")
    name = doc.get("name")
    fn_id = doc.get("id")
    if not path:
        return doc

    tasks = [
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
    ]
    # Traverse `calls` edges from the result's own record id.
    # (A name-based WHERE clause is blocked by the FULLTEXT index on function.name.)
    if fn_id:
        tasks.append(
            _query(
                """SELECT
                     <-calls<-`function`.name      AS callers,
                     <-calls<-`function`.file.path AS caller_files,
                     ->calls->`function`.name      AS callees
                   FROM $fn_id""",
                {"fn_id": fn_id},
            )
        )

    results = await asyncio.gather(*tasks)
    parent_class, siblings = results[0], results[1]
    neighbourhood = results[2] if fn_id else []

    pc_rows = _get_rows(parent_class)
    doc["_parent_class"] = pc_rows[0] if pc_rows else {}
    doc["_siblings"] = [r.get("name") for r in _get_rows(siblings) if r.get("name")]

    nb_rows = _get_rows(neighbourhood)
    nb = nb_rows[0] if nb_rows else {}
    doc["_callers"] = _unique_names(nb.get("callers"))[:_CALL_NEIGHBOUR_LIMIT]
    doc["_caller_files"] = _unique_names(nb.get("caller_files"))[:_CALL_NEIGHBOUR_LIMIT]
    doc["_callees"] = _unique_names(nb.get("callees"))[:_CALL_NEIGHBOUR_LIMIT]

    doc["path"] = path
    return doc


def _unique_names(value) -> list:
    """Normalise a graph-traversal field to a deduplicated list of non-empty strings."""
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    return list(dict.fromkeys(v for v in value if v))


async def _enrich_all(docs: list[dict]) -> list[dict]:
    return await asyncio.gather(*[_enrich_one(doc) for doc in docs])


def _format(doc: dict) -> str:
    name = doc.get("name", "?")
    path = doc.get("path") or "?"
    docstring = (doc.get("docstring") or "").strip()
    suggested = (doc.get("suggested_docstring") or "").strip()
    documented = doc.get("has_docstring", bool(docstring))

    parent = doc.get("_parent_class") or {}
    siblings = doc.get("_siblings") or []
    callers = doc.get("_callers") or []
    callees = doc.get("_callees") or []

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
    if callers:
        lines.append(f"callers:  {', '.join(callers)}")
    if callees:
        lines.append(f"callees:  {', '.join(callees)}")
    if docstring:
        lines.append(f"docstring:          {docstring}")
    elif suggested:
        lines.append(f"suggested docstring: {suggested}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 1: hybrid_search — SurrealDB native RRF (vector + BM25 fusion)
# ---------------------------------------------------------------------------

@traceable(name="hybrid_search", run_type="retriever", process_outputs=_clean)
def _do_hybrid_search(query: str) -> list[str]:
    vec = _embedder.embed_query(query)
    raw_terms = [t for t in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", query)
                 if t.lower() not in _STOP_WORDS and len(t) > 2]
    # Split camelCase/PascalCase identifiers so "DigestAuth" → ["Digest", "Auth"]
    expanded = []
    for t in raw_terms:
        parts = [p for p in re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)', t)
                 if len(p) > 2 and p.lower() not in _STOP_WORDS]
        expanded.extend(parts if parts else [t])
    terms = expanded or raw_terms
    keyword = max(terms, key=len) if terms else query.split()[0]

    rows = asyncio.run(_query_raw(
        """
        LET $vs = SELECT *, file.path AS path,
                         vector::similarity::cosine(embedding, $vec) AS score
                  FROM `function`
                  WHERE embedding <|5,100|> $vec;
        LET $ft = SELECT *, file.path AS path,
                         search::score(0) + search::score(1) + search::score(2) AS score
                  FROM `function`
                  WHERE name @0@ $keyword OR docstring @1@ $keyword OR suggested_docstring @2@ $keyword
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
        SELECT name, diff_status, file.path AS path, has_docstring
        FROM `function`
        WHERE diff_status IS NOT NONE {fn_condition}
        ORDER BY diff_status
        """,
        {"module": module} if module else {},
    )))

    if not file_rows and not fn_rows:
        return version_header + "No version diff data found. Ingest two versions to see what changed."

    files_by_status: dict[str, list[str]] = {"red": [], "yellow": [], "green": [], "added": []}
    for row in file_rows:
        s = row.get("diff_status", "")
        if s in files_by_status:
            files_by_status[s].append(row.get("path", "?"))

    fns_by_status: dict[str, list[str]] = {"red": [], "yellow": [], "green": [], "added": []}
    for row in fn_rows:
        s = row.get("diff_status", "")
        if s in fns_by_status:
            label = f"{row.get('name', '?')} in {row.get('path', '?')}"
            if row.get("has_docstring") is False:
                label += " (undocumented)"
            fns_by_status[s].append(label)

    lines = []
    if version_header:
        lines.append(version_header.rstrip())
    lines.extend(["Version Diff Summary", "=" * 40])

    if files_by_status["added"]:
        lines.append(f"\nNEW files ({len(files_by_status['added'])}):")
        for p in files_by_status["added"]:
            lines.append(f"  + {p}")
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

    if fns_by_status["added"]:
        lines.append(f"\nNEW functions ({len(fns_by_status['added'])}):")
        for f in fns_by_status["added"]:
            lines.append(f"  + {f}")
    if fns_by_status["red"]:
        lines.append(f"\nDELETED functions ({len(fns_by_status['red'])}):")
        for f in fns_by_status["red"]:
            lines.append(f"  - {f}")
    if fns_by_status["yellow"]:
        lines.append(f"\nMODIFIED functions ({len(fns_by_status['yellow'])}):")
        for f in fns_by_status["yellow"]:
            lines.append(f"  ~ {f}")

    total = sum(len(v) for v in files_by_status.values())
    changed = len(files_by_status["yellow"]) + len(files_by_status["red"]) + len(files_by_status["added"])
    lines.append(f"\nTotal: {total} files tracked, {changed} changed, added, or deleted")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 4: list_versions — show ingestion history from SurrealDB
# ---------------------------------------------------------------------------

@tool
@traceable(name="list_versions", run_type="retriever", process_outputs=_clean)
def list_versions(repo_filter: str = "") -> str:
    """List all ingested versions (repositories and their ingestion history).
    Call with no arguments to see everything, or pass a repo name to filter.
    Use when asked what repos are indexed, what versions exist, or ingestion history."""
    condition = "WHERE repo_name CONTAINS $repo" if repo_filter else ""
    rows = _get_rows(asyncio.run(_query(
        f"""
        SELECT repo_path, repo_name, github_url, ingested_at,
               status, file_count, snapshot_path
        FROM ingestion
        {condition}
        ORDER BY ingested_at DESC
        """,
        {"repo": repo_filter} if repo_filter else {},
    )))

    if not rows:
        return "No ingested versions found. Use the UI to ingest a repository first."

    lines = ["Ingested Versions", "=" * 40]
    for i, row in enumerate(rows, 1):
        name = row.get("repo_name", row.get("repo_path", "?"))
        status = row.get("status", "?")
        when = row.get("ingested_at", "?")
        files = row.get("file_count", "?")
        github = row.get("github_url")

        lines.append(f"\n{i}. {name}")
        if github:
            lines.append(f"   source:  {github}")
        lines.append(f"   status:  {status}")
        lines.append(f"   files:   {files}")
        lines.append(f"   date:    {when}")
        if row.get("snapshot_path"):
            lines.append(f"   snapshot: yes")

    lines.append(f"\nTotal: {len(rows)} version(s)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 5: generate_docstring — LLM-generated docstring for undocumented functions
# ---------------------------------------------------------------------------

@tool
@traceable(name="generate_docstring", run_type="chain")
def generate_docstring(function_name: str, file_path: str = "") -> str:
    """Generate a Python docstring for an undocumented function.
    Pass the function name (and optionally file path to disambiguate).
    Use when version_diff reports an undocumented function."""
    condition = "WHERE name CONTAINS $name AND has_docstring = false"
    params: dict = {"name": function_name}
    if file_path:
        condition += " AND file.path CONTAINS $fp"
        params["fp"] = file_path

    rows = _get_rows(asyncio.run(_query(
        f"SELECT name, source, file.path AS path FROM `function` {condition}",
        params,
    )))

    if not rows:
        return f"No undocumented function named '{function_name}' found."

    fn = rows[0]
    source = fn.get("source")
    if not source:
        return f"Function '{function_name}' has no source text stored. Re-ingest to populate."

    path = fn.get("path", "?")
    model = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    client = ollama_client.Client(host=base_url)
    response = client.chat(
        model=model,
        messages=[{
            "role": "user",
            "content": (
                "Generate a concise Python docstring for this function. "
                "Return ONLY the docstring text (no triple quotes, no code).\n\n"
                f"{source}"
            ),
        }],
    )
    docstring = response.message.content.strip()

    return (
        f"Generated docstring for {function_name} ({path}):\n\n"
        f'    """{docstring}"""\n'
    )


# ---------------------------------------------------------------------------
# Tool 6: raise_issue — create a GitHub issue with a code improvement suggestion
# ---------------------------------------------------------------------------

@tool
@traceable(name="raise_issue", run_type="tool")
def raise_issue(title: str, body: str) -> str:
    """Create a GitHub issue with a code improvement suggestion.
    Pass a title and markdown body. Use after generate_docstring to file the suggestion."""
    repo = os.getenv("GITHUB_REPO", "archiemarshall/dead-reckoning")

    result = subprocess.run(
        ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "auth" in stderr.lower() or "login" in stderr.lower():
            return "Error: GitHub CLI is not authenticated. Run 'gh auth login' first."
        return f"Error creating issue: {stderr}"

    url = result.stdout.strip()
    return f"GitHub issue created: {url}"
