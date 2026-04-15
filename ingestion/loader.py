import hashlib
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator

import ollama as ollama_client
from dotenv import load_dotenv
from langsmith import traceable
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


@traceable(name="create_ingestion", run_type="chain")
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


@traceable(name="finalize_ingestion", run_type="chain")
async def finalize_ingestion(
    db: AsyncSurreal,
    ingestion_id: str,
    file_count: int,
    content_hash: str | None = None,
) -> None:
    """Mark an ingestion record as done and record the snapshot path if it exists."""
    from ingestion.snapshot import SNAPSHOT_DIR

    iid_part = ingestion_id.split(":", 1)[1] if ":" in ingestion_id else ingestion_id
    snap_path = SNAPSHOT_DIR / f"{iid_part}.tar"
    snap_str = str(snap_path) if snap_path.exists() else None
    await db.query(
        """UPDATE type::record('ingestion', $iid) SET
           status = 'done', file_count = $fc, content_hash = $ch, snapshot_path = $sp""",
        {"iid": iid_part, "fc": file_count, "ch": content_hash, "sp": snap_str},
    )


async def get_ingestions_for_repo(db: AsyncSurreal, repo_path: str) -> list[dict]:
    """Return all ingestion records for a repo, newest first."""
    rows = _get_rows(await db.query(
        "SELECT id, repo_name, repo_path, github_url, ingested_at, status, file_count, snapshot_path "
        "FROM ingestion WHERE repo_path = $rp ORDER BY ingested_at DESC",
        {"rp": repo_path},
    ))
    return rows


async def get_all_ingestions(db: AsyncSurreal) -> list[dict]:
    """Return all ingestion records grouped by repo, newest first."""
    rows = _get_rows(await db.query(
        "SELECT id, repo_name, repo_path, github_url, ingested_at, status, file_count, snapshot_path "
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

@traceable(name="load_file", run_type="chain")
async def load_file(
    parsed: dict,
    db: AsyncSurreal,
    repo_path: str | None = None,
    ingestion_id: str = "",
    content_hash: str | None = None,
    disk_path: str | None = None,
) -> dict:
    """Upsert one file's nodes and edges for a specific ingestion. Returns count dict.

    repo_path  – canonical identifier written to DB (URL or local path).
    disk_path  – actual filesystem root used to compute folder hierarchy.
                 Defaults to repo_path when not supplied (local repos).
    """
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

    # Upsert repo node
    if repo_path:
        repoid = _repo_id(repo_path, ingestion_id)
        repo_name = os.path.basename(repo_path.rstrip("/"))
        await db.query(
            "UPSERT type::record('repo', $id) SET path = $path, name = $name, ingestion_id = $iid",
            {"id": repoid, "path": repo_path, "name": repo_name, "iid": ingestion_id},
        )

    # Build full folder hierarchy from file up to repo root.
    # disk_path is the filesystem root; repo_path is the canonical DB identifier.
    # For local repos they are the same.  For GitHub clones disk_path is the
    # temp clone dir while repo_path is the original URL.
    if repo_path:
        fs_root = (disk_path or repo_path).rstrip("/")
        file_dir = os.path.dirname(path)

        # Collect intermediate directories from immediate parent up to (not including) fs_root.
        # folder_chain[0] = immediate parent, folder_chain[-1] = direct child of fs_root.
        fs_root_norm = os.path.normpath(fs_root)
        folder_chain: list[str] = []
        curr = file_dir
        while curr:
            if os.path.normpath(curr) == fs_root_norm:
                break
            folder_chain.append(curr)
            parent = os.path.dirname(curr)
            if parent == curr:  # filesystem root – stop
                break
            curr = parent

        # Create all folder nodes
        for fp in folder_chain:
            fid_f = _folder_id(fp, ingestion_id)
            await db.query(
                "UPSERT type::record('folder', $id) SET path = $path, ingestion_id = $iid",
                {"id": fid_f, "path": fp, "iid": ingestion_id},
            )

        # file → in_folder → immediate parent folder
        if folder_chain:
            immediate_fid = _folder_id(folder_chain[0], ingestion_id)
            eid = _edge_id(fid, "in_folder", immediate_fid)
            await db.query(
                "INSERT RELATION INTO in_folder { id: type::record('in_folder', $eid), in: type::record('file', $fid), out: type::record('folder', $fid_f) } ON DUPLICATE KEY UPDATE in = in",
                {"eid": eid, "fid": fid, "fid_f": immediate_fid},
            )

            # Chain intermediate folders: folder[i] → in_folder → folder[i+1]
            for i in range(len(folder_chain) - 1):
                child_fid  = _folder_id(folder_chain[i],     ingestion_id)
                parent_fid = _folder_id(folder_chain[i + 1], ingestion_id)
                eid = _edge_id(child_fid, "in_folder", parent_fid)
                await db.query(
                    "INSERT RELATION INTO in_folder { id: type::record('in_folder', $eid), in: type::record('folder', $cfid), out: type::record('folder', $pfid) } ON DUPLICATE KEY UPDATE in = in",
                    {"eid": eid, "cfid": child_fid, "pfid": parent_fid},
                )

            # Top-level folder (direct child of repo root) → in_repo → repo
            top_fid = _folder_id(folder_chain[-1], ingestion_id)
            eid = _edge_id(top_fid, "in_repo", repoid)
            await db.query(
                "INSERT RELATION INTO in_repo { id: type::record('in_repo', $eid), in: type::record('folder', $folderid), out: type::record('repo', $repoid) } ON DUPLICATE KEY UPDATE in = in",
                {"eid": eid, "folderid": top_fid, "repoid": repoid},
            )
        else:
            # File is directly in the repo root – connect file straight to repo
            eid = _edge_id(fid, "in_repo", repoid)
            await db.query(
                "INSERT RELATION INTO in_repo { id: type::record('in_repo', $eid), in: type::record('file', $fid), out: type::record('repo', $repoid) } ON DUPLICATE KEY UPDATE in = in",
                {"eid": eid, "fid": fid, "repoid": repoid},
            )

    fn_count = 0
    class_count = 0
    edge_count = 0

    # Batch-embed function docstrings via async Ollama client, reusing cached
    # embeddings for functions whose source hasn't changed (same content_hash).
    embed_model = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    embed_host = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    fns_with_docs = [(i, fn) for i, fn in enumerate(parsed["functions"]) if fn.get("docstring")]
    embeddings_map: dict[int, list[float]] = {}
    if fns_with_docs:
        # Check DB for existing embeddings by content_hash to avoid redundant Ollama calls
        source_hashes = [fn.get("source_hash") for _, fn in fns_with_docs if fn.get("source_hash")]
        cached: dict[str, list[float]] = {}
        if source_hashes:
            rows = await db.query(
                "SELECT content_hash, embedding FROM `function` WHERE content_hash IN $hashes AND embedding IS NOT NONE",
                {"hashes": source_hashes},
            )
            for row in _get_rows(rows):
                if row.get("content_hash") and row.get("embedding"):
                    cached[row["content_hash"]] = row["embedding"]

        # Split into cached hits and functions that need embedding
        need_embed: list[tuple[int, dict]] = []
        for i, fn in fns_with_docs:
            sh = fn.get("source_hash")
            if sh and sh in cached:
                embeddings_map[i] = cached[sh]
            else:
                need_embed.append((i, fn))

        if need_embed:
            docs = [_strip_markdown(fn["docstring"]) for _, fn in need_embed]
            client = ollama_client.AsyncClient(host=embed_host)
            response = await client.embed(model=embed_model, input=docs)
            vecs = response.embeddings
            for (i, _), vec in zip(need_embed, vecs):
                embeddings_map[i] = vec

        reused = len(fns_with_docs) - len(need_embed)
        print(f"  Embeddings: reused {reused} cached, embedded {len(need_embed)} new")

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
               ingestion_id = $iid, content_hash = $ch,
               source = $source""",
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
                "ch": fn.get("source_hash"),
                "source": fn.get("source"),
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
    class_id_map: dict[str, str] = {}  # class_name -> bare record ID
    for cls in parsed["classes"]:
        clsid = _class_id(path, cls["name"], ingestion_id)
        class_id_map[cls["name"]] = clsid
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

    # Create inherits edges (class -> base class) for bases defined in the same file
    for cls in parsed["classes"]:
        child_clsid = class_id_map[cls["name"]]
        for base_name in cls.get("bases", []):
            if base_name in class_id_map:
                parent_clsid = class_id_map[base_name]
                eid = _edge_id(child_clsid, "inherits", parent_clsid)
                await db.query(
                    "INSERT RELATION INTO inherits { id: type::record('inherits', $eid), in: type::record('class', $child), out: type::record('class', $parent) } ON DUPLICATE KEY UPDATE in = in",
                    {"eid": eid, "child": child_clsid, "parent": parent_clsid},
                )
                edge_count += 1

    return {"functions": fn_count, "classes": class_count, "edges": edge_count}


# ---------------------------------------------------------------------------
# Second-pass: create calls edges across all ingested files
# ---------------------------------------------------------------------------

@traceable(name="load_calls", run_type="chain")
async def load_calls(parsed_files: list[dict], db: AsyncSurreal, ingestion_id: str = "") -> dict:
    """Create function→calls→function edges and file→imports→file edges (second pass).

    Must be called after all files are loaded so callee/importee nodes already exist.
    Returns a dict ``{"calls": int, "imports": int}`` with per-edge-type counts.
    """
    # Collect all unique callee names referenced across every function
    all_callee_names: set[str] = set()
    for parsed in parsed_files:
        for fn in parsed.get("functions", []):
            all_callee_names.update(fn.get("calls") or [])

    calls_count = 0
    imports_count = 0

    # --- Function calls edges ---
    if all_callee_names:
        # Fetch all function nodes in this ingestion and filter in Python.
        # A direct `WHERE name IN $names` clause is intercepted by the BM25
        # FULLTEXT index on function.name and returns zero rows.
        query = "SELECT id, name FROM `function`"
        params: dict = {}
        if ingestion_id:
            query += " WHERE ingestion_id = $iid"
            params["iid"] = ingestion_id
        rows = await db.query(query, params)
        # Unwrap SurrealDB response format
        if isinstance(rows, list) and rows and isinstance(rows[0], dict) and "result" in rows[0]:
            rows = rows[0].get("result") or []

        # Build name → list of bare record IDs (strip "function:abc123" → "abc123")
        callee_map: dict[str, list[str]] = {}
        for row in (rows or []):
            name = row.get("name")
            if name not in all_callee_names:
                continue
            rid = str(row.get("id", ""))
            bare = rid.split(":")[-1] if ":" in rid else rid
            if name and bare:
                callee_map.setdefault(name, []).append(bare)

        for parsed in parsed_files:
            file_path = parsed["path"]
            for fn in parsed.get("functions", []):
                caller_bare = _function_id(file_path, fn.get("class_name"), fn["name"], ingestion_id)
                for callee_name in (fn.get("calls") or []):
                    for callee_bare in callee_map.get(callee_name, []):
                        eid = _edge_id(caller_bare, "calls", callee_bare)
                        await db.query(
                            "INSERT RELATION INTO calls { id: type::record('calls', $eid), in: type::record('function', $caller), out: type::record('function', $callee) } ON DUPLICATE KEY UPDATE in = in",
                            {"eid": eid, "caller": caller_bare, "callee": callee_bare},
                        )
                        calls_count += 1

    # --- File imports edges ---
    # Build a map of module_name → file bare ID for matching imports to files
    file_id_by_stem: dict[str, str] = {}
    for parsed in parsed_files:
        stem = os.path.splitext(os.path.basename(parsed["path"]))[0]
        fid = _file_id(parsed["path"], ingestion_id)
        file_id_by_stem[stem] = fid

    for parsed in parsed_files:
        from_fid = _file_id(parsed["path"], ingestion_id)
        for imp in parsed.get("imports", []):
            # Match against the final component of the import path
            module_stem = imp.rsplit(".", 1)[-1]
            to_fid = file_id_by_stem.get(module_stem)
            if to_fid and to_fid != from_fid:
                eid = _edge_id(from_fid, "imports", to_fid)
                await db.query(
                    "INSERT RELATION INTO imports { id: type::record('imports', $eid), in: type::record('file', $from_fid), out: type::record('file', $to_fid) } ON DUPLICATE KEY UPDATE in = in",
                    {"eid": eid, "from_fid": from_fid, "to_fid": to_fid},
                )
                imports_count += 1

    return {"calls": calls_count, "imports": imports_count}
