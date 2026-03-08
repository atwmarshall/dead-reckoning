"""Post-ingestion enrichment: suggest docstrings for undocumented functions.

For every function where has_docstring = false (and suggested_docstring is not yet set),
this module:
  1. Gathers source code + sibling context from SurrealDB
  2. Calls the local LLM to produce a suggested Python docstring
  3. Re-embeds "{name}: {docstring}" and writes both fields back to SurrealDB

Run automatically at the end of seed.py, or call enrich_functions() directly.
"""

import os
import re

import ollama as ollama_client
from langchain_ollama import ChatOllama
from surrealdb import AsyncSurreal


def _clean_docstring(text: str) -> str:
    """Strip triple quotes, markdown fences, and leading/trailing whitespace."""
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```$", "", text).strip()
    text = re.sub(r'^"""', "", text).strip()
    text = re.sub(r'"""$', "", text).strip()
    text = re.sub(r"^'''", "", text).strip()
    text = re.sub(r"'''$", "", text).strip()
    return text.strip()


async def enrich_functions(
    db: AsyncSurreal,
    *,
    batch_size: int = 20,
    force: bool = False,
) -> int:
    """Suggest docstrings for undocumented functions and write them back to SurrealDB.

    Args:
        db: Open, authenticated SurrealDB client.
        batch_size: How many functions to process per LLM/embed batch.
        force: If True, re-enrich functions that already have a suggested_docstring.

    Returns:
        Number of functions enriched.
    """
    # Self-migrate: ensure the field exists even if apply_schema hasn't been re-run
    await db.query(
        "DEFINE FIELD IF NOT EXISTS suggested_docstring ON `function` TYPE option<string>"
    )

    # Fetch targets — include source so the LLM has actual code to work from
    where = "has_docstring = false" if force else "has_docstring = false AND suggested_docstring IS NONE"
    rows = await db.query(
        f"SELECT id, name, class_name, file.path AS path, source FROM `function` WHERE {where}",
    )
    if isinstance(rows, list) and rows and isinstance(rows[0], dict) and "result" in rows[0]:
        rows = rows[0].get("result") or []
    rows = rows or []

    if not rows:
        print("Enrich: no undocumented functions to process.")
        return 0

    total = len(rows)
    print(f"Enrich: {total} undocumented function(s) to document.")

    llm = ChatOllama(
        model=os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    )
    embed_model = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    embed_host = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    embed_client = ollama_client.AsyncClient(host=embed_host)

    enriched = 0

    for batch_start in range(0, total, batch_size):
        batch = rows[batch_start : batch_start + batch_size]

        # Gather sibling names for functions that have no source stored
        sibling_map: dict[str, list[str]] = {}
        for fn in batch:
            if fn.get("source"):
                continue  # source gives the LLM enough context
            fn_id = str(fn.get("id", ""))
            sibling_rows = await db.query(
                "SELECT name FROM `function` WHERE file.path = $path AND class_name = $cn AND name != $name LIMIT 10",
                {"path": fn.get("path") or "", "cn": fn.get("class_name"), "name": fn.get("name", "")},
            )
            if isinstance(sibling_rows, list) and sibling_rows and "result" in sibling_rows[0]:
                sibling_rows = sibling_rows[0].get("result") or []
            sibling_map[fn_id] = [r.get("name") for r in (sibling_rows or []) if r.get("name")]

        # Generate docstring for each function
        docstrings: dict[str, str] = {}
        for fn in batch:
            fn_id = str(fn.get("id", ""))
            name = fn.get("name", "?")
            source = (fn.get("source") or "").strip()

            if source:
                # Best case: LLM sees the actual source code
                user_msg = (
                    "Generate a concise Python docstring for this function. "
                    "Return ONLY the docstring text — no triple quotes, no code fences, no preamble.\n\n"
                    f"{source}"
                )
            else:
                # Fallback: use name, class, file, and sibling names as context
                class_name = fn.get("class_name")
                path = fn.get("path") or "unknown"
                siblings = sibling_map.get(fn_id, [])
                parts = [f"Function `{name}`"]
                if class_name:
                    parts.append(f"in class `{class_name}`")
                parts.append(f"(file: {path}).")
                if siblings:
                    parts.append(f"Sibling functions: {', '.join(siblings)}.")
                parts.append(
                    "Suggest a concise Python docstring for this function. "
                    "Return ONLY the docstring text — no triple quotes, no code fences."
                )
                user_msg = " ".join(parts)

            try:
                response = llm.invoke([
                    ("system", "You are a Python documentation assistant. Write clear, concise docstrings."),
                    ("human", user_msg),
                ])
                docstring = _clean_docstring(str(response.content))
            except Exception as exc:
                print(f"  LLM error for {name}: {exc}")
                docstring = f"Performs the {name} operation."

            docstrings[fn_id] = docstring

        # Batch-embed "{name}: {docstring}" so semantic search can find these functions
        embed_inputs = [
            f"{fn.get('name', '')}: {docstrings[str(fn.get('id', ''))]}"
            for fn in batch
        ]
        try:
            embed_response = await embed_client.embed(model=embed_model, input=embed_inputs)
            embeddings = embed_response.embeddings
        except Exception as exc:
            print(f"  Embed error (batch {batch_start}): {exc}")
            embeddings = [None] * len(batch)

        # Write suggested_docstring + refreshed embedding back to SurrealDB
        for fn, embedding in zip(batch, embeddings):
            fn_id = str(fn.get("id", ""))
            bare = fn_id.split(":")[-1] if ":" in fn_id else fn_id
            await db.query(
                "UPDATE type::record('function', $id) SET suggested_docstring = $doc, embedding = $embedding",
                {"id": bare, "doc": docstrings[fn_id], "embedding": embedding},
            )
            enriched += 1

        done = min(batch_start + batch_size, total)
        print(f"  [{done}/{total}] documented")

    return enriched