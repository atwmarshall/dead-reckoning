import hashlib
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
# Deterministic record IDs (all include ingestion_id for version isolation)
# ---------------------------------------------------------------------------

def _file_id(path: str, ingestion_id: str) -> str:
    return hashlib.md5((path + ingestion_id).encode()).hexdigest()[:12]


def _function_id(file_path: str, class_name: str | None, fn_name: str, ingestion_id: str) -> str:
    return hashlib.md5(f"{file_path}::{class_name or ''}::{fn_name}::{ingestion_id}".encode()).hexdigest()[:12]


def _class_id(file_path: str, class_name: str, ingestion_id: str) -> str:
    return hashlib.md5(f"{file_path}::{class_name}::{ingestion_id}".encode()).hexdigest()[:12]


def _folder_id(path: str, ingestion_id: str) -> str:
    return hashlib.md5((path + ingestion_id).encode()).hexdigest()[:12]


def _repo_id(path: str, ingestion_id: str) -> str:
    return hashlib.md5((path + ingestion_id).encode()).hexdigest()[:12]


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
# Ingestion record management
# ---------------------------------------------------------------------------

def _get_rows(result) -> list:
    if isinstance(result, list):
        if result and isinstance(result[0], dict) and "result" in result[0]:
            return result[0].get("result") or []
        return result
    return []


async def create_ingestion(
    db: AsyncSurreal,
    repo_path: str,
    github_url: str | None = None,
) -> str:
    """Create an ingestion record and return its ID string (e.g. 'ingestion:abc')."""
    iid = uuid.uuid4().hex[:16]
    repo_name = os.path.basename(repo_path.rstrip("/"))
    now = datetime.now(timezone.utc).isoformat()
    await db.query(
        """CREATE type::record('ingestion', $iid) SET
           repo_path = $repo_path, repo_name = $repo_name,
           github_url = $github_url, ingested_at = type::datetime($now),
           status = 'running'""",
        {"iid": iid, "repo_path": repo_path, "repo_name": repo_name,
         "github_url": github_url, "now": now},
    )
    return f"ingestion:{iid}"


async def finalize_ingestion(
    db: AsyncSurreal,
    ingestion_id: str,
    file_count: int,
    content_hash: str | None = None,
) -> None:
    """Mark an ingestion record as done."""
    iid_part = ingestion_id.split(":", 1)[1] if ":" in ingestion_id else ingestion_id
    await db.query(
        """UPDATE type::record('ingestion', $iid) SET
           status = 'done', file_count = $fc, content_hash = $ch""",
        {"iid": iid_part, "fc": file_count, "ch": content_hash},
    )


async def get_ingestions_for_repo(db: AsyncSurreal, repo_path: str) -> list[dict]:
    """Return all ingestion records for a repo, newest first."""
    rows = _get_rows(await db.query(
        "SELECT id, repo_name, repo_path, github_url, ingested_at, status, file_count "
        "FROM ingestion WHERE repo_path = $rp ORDER BY ingested_at DESC",
        {"rp": repo_path},
    ))
    return rows


async def get_all_ingestions(db: AsyncSurreal) -> list[dict]:
    """Return all ingestion records grouped by repo, newest first."""
    rows = _get_rows(await db.query(
        "SELECT id, repo_name, repo_path, github_url, ingested_at, status, file_count "
        "FROM ingestion ORDER BY ingested_at DESC"
    ))
    return rows


async def delete_ingestion(db: AsyncSurreal, ingestion_id: str) -> None:
    """Delete all nodes and edges belonging to an ingestion, then the record itself."""
    iid = ingestion_id  # e.g. "ingestion:abc123"
    iid_part = iid.split(":", 1)[1] if ":" in iid else iid

    # Delete edges (filter via linked node's ingestion_id)
    for edge_table in ("contains", "in_folder", "in_repo", "imports", "calls", "inherits"):
        await db.query(
            f"DELETE {edge_table} WHERE in.ingestion_id = $iid",
            {"iid": iid},
        )

    # Delete node records
    for table in ("`function`", "`class`", "file", "folder", "repo"):
        await db.query(
            f"DELETE {table} WHERE ingestion_id = $iid",
            {"iid": iid},
        )

    # Delete the ingestion record itself
    await db.query(
        "DELETE type::record('ingestion', $iid_part)",
        {"iid_part": iid_part},
    )


# ---------------------------------------------------------------------------
# Load a single parsed file into SurrealDB
# ---------------------------------------------------------------------------

async def load_file(
    parsed: dict,
    db: AsyncSurreal,
    repo_path: str | None = None,
    ingestion_id: str = "",
    content_hash: str | None = None,
) -> dict:
    """Upsert one file's nodes and edges for a specific ingestion. Returns count dict."""
    path = parsed["path"]
    fid = _file_id(path, ingestion_id)

    # Upsert file node
    await db.query(
        """UPSERT type::record('file', $id) SET
           path = $path, line_count = $lc, language = 'python',
           ingestion_id = $iid, content_hash = $ch""",
        {"id": fid, "path": path, "lc": parsed["line_count"],
         "iid": ingestion_id, "ch": content_hash},
    )

    # Upsert repo node + in_repo edge
    if repo_path:
        repoid = _repo_id(repo_path, ingestion_id)
        repo_name = os.path.basename(repo_path.rstrip("/"))
        await db.query(
            "UPSERT type::record('repo', $id) SET path = $path, name = $name, ingestion_id = $iid",
            {"id": repoid, "path": repo_path, "name": repo_name, "iid": ingestion_id},
        )

    # Upsert parent folder node + in_folder edge
    folder_path = os.path.dirname(path)
    folderid = _folder_id(folder_path, ingestion_id)
    await db.query(
        "UPSERT type::record('folder', $id) SET path = $path, ingestion_id = $iid",
        {"id": folderid, "path": folder_path, "iid": ingestion_id},
    )
    eid = _edge_id(fid, "in_folder", folderid)
    await db.query(
        "INSERT RELATION INTO in_folder { id: type::record('in_folder', $eid), in: type::record('file', $fid), out: type::record('folder', $folderid) } ON DUPLICATE KEY UPDATE in = in",
        {"eid": eid, "fid": fid, "folderid": folderid},
    )

    if repo_path:
        eid = _edge_id(folderid, "in_repo", repoid)
        await db.query(
            "INSERT RELATION INTO in_repo { id: type::record('in_repo', $eid), in: type::record('folder', $folderid), out: type::record('repo', $repoid) } ON DUPLICATE KEY UPDATE in = in",
            {"eid": eid, "folderid": folderid, "repoid": repoid},
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
        class_name = fn.get("class_name")
        fnid = _function_id(path, class_name, fn["name"], ingestion_id)
        await db.query(
            """UPSERT type::record('function', $id) SET
               name = $name, file = type::record('file', $fid),
               lineno = $lineno, docstring = $docstring,
               has_docstring = $has_docstring, class_name = $class_name,
               is_method = $is_method, embedding = $embedding,
               ingestion_id = $iid""",
            {
                "id": fnid,
                "name": fn["name"],
                "fid": fid,
                "lineno": fn["lineno"],
                "docstring": fn.get("docstring"),
                "has_docstring": bool(fn.get("docstring")),
                "class_name": class_name,
                "is_method": class_name is not None,
                "embedding": embeddings_map.get(idx),
                "iid": ingestion_id,
            },
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
        clsid = _class_id(path, cls["name"], ingestion_id)
        await db.query(
            """UPSERT type::record('class', $id) SET
               name = $name, file = type::record('file', $fid),
               lineno = $lineno, bases = $bases, ingestion_id = $iid""",
            {"id": clsid, "name": cls["name"], "fid": fid,
             "lineno": cls["lineno"], "bases": cls["bases"], "iid": ingestion_id},
        )
        eid = _edge_id(fid, "contains", clsid)
        await db.query(
            "INSERT RELATION INTO contains { id: type::record('contains', $eid), in: type::record('file', $fid), out: type::record('class', $clsid) } ON DUPLICATE KEY UPDATE in = in",
            {"eid": eid, "fid": fid, "clsid": clsid},
        )
        class_count += 1
        edge_count += 1

    return {"functions": fn_count, "classes": class_count, "edges": edge_count}
