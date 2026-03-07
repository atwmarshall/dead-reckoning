import hashlib
import os
import re
from contextlib import asynccontextmanager
from typing import AsyncIterator

import ollama as ollama_client
from dotenv import load_dotenv
from surrealdb import AsyncSurreal

load_dotenv()


def _strip_markdown(text: str) -> str:
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = re.sub(r'`[^`]+`', '', text)
    text = re.sub(r'\*+', '', text)
    return text.strip()


# ---------------------------------------------------------------------------
# Deterministic record IDs
# ---------------------------------------------------------------------------

def _file_id(path: str) -> str:
    return hashlib.md5(path.encode()).hexdigest()[:12]


def _function_id(file_path: str, fn_name: str) -> str:
    return hashlib.md5(f"{file_path}::{fn_name}".encode()).hexdigest()[:12]


def _class_id(file_path: str, class_name: str) -> str:
    return hashlib.md5(f"{file_path}::{class_name}".encode()).hexdigest()[:12]


def _folder_id(path: str) -> str:
    return hashlib.md5(path.encode()).hexdigest()[:12]


def _edge_id(from_id: str, rel: str, to_id: str) -> str:
    return hashlib.md5(f"{from_id}->{rel}->{to_id}".encode()).hexdigest()[:12]



# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

@asynccontextmanager
async def get_db_client() -> AsyncIterator[AsyncSurreal]:
    url = os.environ["SURREALDB_URL"]
    user = os.environ["SURREALDB_USER"]
    password = os.environ["SURREALDB_PASS"]
    ns = os.environ["SURREALDB_NS"]
    db_name = os.environ["SURREALDB_DB"]

    db = AsyncSurreal(url)
    await db.connect()
    await db.signin({"username": user, "password": password})
    await db.use(ns, db_name)
    try:
        yield db
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Load a single parsed file into SurrealDB
# ---------------------------------------------------------------------------

async def load_file(parsed: dict, db: AsyncSurreal) -> dict:
    """Upsert one file's nodes and edges. Returns count dict."""
    path = parsed["path"]
    fid = _file_id(path)

    # Upsert file node
    await db.query(
        "UPSERT type::record('file', $id) SET path = $path, line_count = $lc, language = 'python'",
        {"id": fid, "path": path, "lc": parsed["line_count"]},
    )

    # Upsert parent folder node + in_folder edge
    folder_path = os.path.dirname(path)
    folderid = _folder_id(folder_path)
    await db.query(
        "UPSERT type::record('folder', $id) SET path = $path",
        {"id": folderid, "path": folder_path},
    )
    eid = _edge_id(fid, "in_folder", folderid)
    await db.query(
        "INSERT RELATION INTO in_folder { id: type::record('in_folder', $eid), in: type::record('file', $fid), out: type::record('folder', $folderid) } ON DUPLICATE KEY UPDATE in = in",
        {"eid": eid, "fid": fid, "folderid": folderid},
    )

    fn_count = 0
    class_count = 0
    edge_count = 0

    # Batch-embed function docstrings via async Ollama client
    embed_model = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    embed_host = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    fns_with_docs = [(i, fn) for i, fn in enumerate(parsed["functions"]) if fn.get("docstring")]
    embeddings_map: dict[int, list[float]] = {}
    if fns_with_docs:
        docs = [_strip_markdown(fn["docstring"]) for _, fn in fns_with_docs]
        client = ollama_client.AsyncClient(host=embed_host)
        response = await client.embed(model=embed_model, input=docs)
        vecs = response.embeddings
        embeddings_map = {i: vec for (i, _), vec in zip(fns_with_docs, vecs)}

    # Upsert functions + contains edges
    for idx, fn in enumerate(parsed["functions"]):
        fnid = _function_id(path, fn["name"])
        await db.query(
            """UPSERT type::record('function', $id) SET
               name = $name, file = type::record('file', $fid),
               lineno = $lineno, docstring = $docstring, is_method = false,
               embedding = $embedding""",
            {"id": fnid, "name": fn["name"], "fid": fid,
             "lineno": fn["lineno"], "docstring": fn.get("docstring"),
             "embedding": embeddings_map.get(idx)},
        )
        eid = _edge_id(fid, "contains", fnid)
        await db.query(
            "INSERT RELATION INTO contains { id: type::record('contains', $eid), in: type::record('file', $fid), out: type::record('function', $fnid) } ON DUPLICATE KEY UPDATE in = in",
            {"eid": eid, "fid": fid, "fnid": fnid},
        )
        fn_count += 1
        edge_count += 1

    # Upsert classes + contains edges
    for cls in parsed["classes"]:
        clsid = _class_id(path, cls["name"])
        await db.query(
            """UPSERT type::record('class', $id) SET
               name = $name, file = type::record('file', $fid),
               lineno = $lineno, bases = $bases""",
            {"id": clsid, "name": cls["name"], "fid": fid,
             "lineno": cls["lineno"], "bases": cls["bases"]},
        )
        eid = _edge_id(fid, "contains", clsid)
        await db.query(
            "INSERT RELATION INTO contains { id: type::record('contains', $eid), in: type::record('file', $fid), out: type::record('class', $clsid) } ON DUPLICATE KEY UPDATE in = in",
            {"eid": eid, "fid": fid, "clsid": clsid},
        )
        class_count += 1
        edge_count += 1

    return {"functions": fn_count, "classes": class_count, "edges": edge_count}
