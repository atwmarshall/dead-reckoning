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


_STOP_WORDS = {
    "what", "does", "do", "the", "a", "an", "function", "method", "class",
    "tell", "me", "about", "how", "is", "are", "in", "for", "to", "of",
    "and", "or", "it", "can", "you", "give", "show", "explain", "get",
}


def _clean(obj):
    """Strip embedding arrays and internal keys for readable LangSmith traces."""
    if isinstance(obj, list):
        return [_clean(x) for x in obj]
    if isinstance(obj, dict):
        return {
            k: _clean(v)
            for k, v in obj.items()
            if k not in ("self", "embedding", "score", "id")
        }
    return obj


class DeadReckoningRetriever:
    """Hybrid retriever combining vector similarity and keyword graph search
    via Reciprocal Rank Fusion (RRF)."""

    def __init__(self, k: int = 60, limit: int = 5, semantic_threshold: float = 0.65):
        self.k = k
        self.limit = limit
        self.semantic_threshold = semantic_threshold
        self._embedder = OllamaEmbeddings(
            model=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text"),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        )

    def _extract_terms(self, query: str) -> list[str]:
        tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", query.lower())
        return [t for t in tokens if t not in _STOP_WORDS and len(t) > 2]

    @traceable(
        name="semantic_search",
        run_type="retriever",
        process_inputs=lambda x: {"vec_dims": len(x.get("vec", [])), "threshold": x.get("threshold", 0.65)},
        process_outputs=_clean,
    )
    async def _semantic(self, vec: list[float], threshold: float = 0.65) -> list[dict]:
        rows = await _query(
            """SELECT id, name, lineno, class_name, docstring, has_docstring, file.path AS path,
               vector::similarity::cosine(embedding, $vec) AS score
               FROM `function`
               WHERE embedding IS NOT NONE
               AND vector::similarity::cosine(embedding, $vec) >= $threshold
               ORDER BY score DESC
               LIMIT 10""",
            {"vec": vec, "threshold": threshold},
        )
        return rows or []

    @traceable(
        name="keyword_search",
        run_type="retriever",
        process_inputs=lambda x: {"terms": x.get("terms", [])},
        process_outputs=_clean,
    )
    async def _keyword(self, terms: list[str]) -> list[dict]:
        if not terms:
            return []
        conditions = " OR ".join(
            f"string::lowercase(name) CONTAINS $t{i}" for i in range(len(terms))
        )
        vars = {f"t{i}": term for i, term in enumerate(terms)}
        rows = await _query(
            f"""SELECT id, name, lineno, class_name, docstring, has_docstring, file.path AS path
                FROM `function`
                WHERE {conditions}
                LIMIT 25""",
            vars,
        )
        return rows or []

    @traceable(
        name="rrf_merge",
        run_type="chain",
        process_inputs=lambda x: {
            "exact_terms": list(x.get("exact_terms") or []),
            "list_counts": [len(lst) for lst in x.get("ranked_lists", [])],
        },
        process_outputs=_clean,
    )
    def _rrf_merge(self, *ranked_lists: list[dict], exact_terms: set[str] | None = None) -> list[dict]:
        scores: dict[str, float] = {}
        docs: dict[str, dict] = {}
        for ranked in ranked_lists:
            for rank, doc in enumerate(ranked):
                key = str(doc.get("id") or f"{doc.get('name')}::{doc.get('path')}")
                scores[key] = scores.get(key, 0.0) + 1.0 / (self.k + rank + 1)
                if exact_terms and doc.get("name", "").lower() in exact_terms:
                    scores[key] += 1.0 / self.k
                docs[key] = doc
        return [docs[k] for k in sorted(docs, key=lambda k: scores[k], reverse=True)]

    @traceable(
        name="graph_enrich",
        run_type="retriever",
        process_inputs=lambda x: {
            "function": x.get("doc", {}).get("name"),
            "path": x.get("doc", {}).get("path"),
            "lineno": x.get("doc", {}).get("lineno"),
        },
        process_outputs=lambda x: {
            "function": x.get("name"),
            "parent_class": x.get("_parent_class", {}).get("name"),
            "siblings": x.get("_siblings", []),
        },
    )
    async def _enrich(self, doc: dict) -> dict:
        """Add graph context: inferred parent class and sibling function names."""
        path = doc.get("path")
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

        doc["_parent_class"] = (parent_class or [{}])[0] or {}
        doc["_siblings"] = [r.get("name") for r in (siblings or []) if r.get("name")]
        return doc

    @staticmethod
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

    async def retrieve(self, query: str) -> list[str]:
        terms = self._extract_terms(query)
        vec = self._embedder.embed_query(query)
        semantic_results, keyword_results = await asyncio.gather(
            self._semantic(vec, threshold=self.semantic_threshold),
            self._keyword(terms),
        )
        merged = self._rrf_merge(semantic_results, keyword_results, exact_terms=set(terms))

        # If the top results all share the same function name (multiple implementations),
        # return all of them rather than cutting off. Otherwise apply the normal limit.
        top_name = merged[0].get("name") if merged else None
        if top_name and all(d.get("name") == top_name for d in merged[:self.limit]):
            cutoff = sum(1 for d in merged if d.get("name") == top_name)
        else:
            cutoff = self.limit

        enriched = await asyncio.gather(*[self._enrich(doc) for doc in merged[:cutoff]])
        results = [self._format(doc) for doc in enriched]
        if cutoff == self.limit and len(merged) > self.limit:
            results.append(
                f"note: results truncated at {self.limit}. "
                f"Refine your query or use explain_module with a filename to explore further."
            )
        return results


_retriever = DeadReckoningRetriever()


@tool
def hybrid_search(query: str) -> list[str]:
    """Search the codebase combining semantic similarity and keyword name matching.
    Use for any question about what a function does, finding functions by name,
    or exploring codebase concepts."""
    return asyncio.run(_retriever.retrieve(query))


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
