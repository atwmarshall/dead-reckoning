import hashlib
import os
from pathlib import Path
from typing import AsyncGenerator

from langsmith import traceable

from ingestion.snapshot import SNAPSHOT_DIR, diff_snapshots

_SKIP = {"__pycache__", ".git", "venv", ".venv", "node_modules", ".tox"}


def content_hash_file(path: str) -> str:
    """SHA-256 of file contents."""
    data = Path(path).read_bytes()
    return hashlib.sha256(data).hexdigest()


def content_hash_folder(child_hashes: list[str]) -> str:
    """Hash of sorted child hashes."""
    combined = "".join(sorted(child_hashes)).encode()
    return hashlib.sha256(combined).hexdigest()


def compute_repo_hash(file_hash_map: dict[str, str]) -> str:
    """Overall hash from all file hashes."""
    return content_hash_folder(list(file_hash_map.values()))


def _file_node_id(path: str, ingestion_id: str) -> str:
    return f"file:{hashlib.md5((path + ingestion_id).encode()).hexdigest()[:12]}"


def _function_node_id(file_path: str, class_name: str | None, fn_name: str, ingestion_id: str) -> str:
    key = f"{file_path}::{class_name or ''}::{fn_name}::{ingestion_id}"
    return f"function:{hashlib.md5(key.encode()).hexdigest()[:12]}"


def _get_rows(result) -> list:
    if isinstance(result, list):
        if result and isinstance(result[0], dict) and "result" in result[0]:
            return result[0].get("result") or []
        return result
    return []


class DiffEngine:
    @staticmethod
    async def _diff_functions(
        db,
        prev_ingestion_id: str,
        file_path: str,
        file_status: str,
        new_disk_path: str | None = None,
    ) -> list[dict]:
        """Compare function-level content_hash for a single file.

        Returns list of {"node_id", "status", "name"} events.
        For 'red' files, all functions are red.
        For 'yellow' files, compare by (name, class_name) key.
        For 'green' files, all functions are green.
        """
        # Get prev functions for this file
        file_rid = _file_node_id(file_path, prev_ingestion_id)
        prev_fns = _get_rows(await db.query(
            "SELECT id, name, class_name, content_hash FROM `function` WHERE ingestion_id = $iid AND file = type::record('file', $frid)",
            {"iid": prev_ingestion_id, "frid": file_rid.split(":", 1)[1]},
        ))

        if not prev_fns:
            return []

        events = []

        if file_status == "red":
            for fn in prev_fns:
                fn_nid = str(fn["id"])
                rid = fn_nid.split(":", 1)[1] if ":" in fn_nid else fn_nid
                await db.query(
                    "UPDATE type::record('function', $rid) SET diff_status = $s",
                    {"rid": rid, "s": "red"},
                )
                events.append({"node_id": fn_nid, "status": "red", "name": fn.get("name", "")})
        elif file_status == "green":
            for fn in prev_fns:
                fn_nid = str(fn["id"])
                rid = fn_nid.split(":", 1)[1] if ":" in fn_nid else fn_nid
                await db.query(
                    "UPDATE type::record('function', $rid) SET diff_status = $s",
                    {"rid": rid, "s": "green"},
                )
                events.append({"node_id": fn_nid, "status": "green", "name": fn.get("name", "")})
        elif file_status == "yellow":
            # Parse the new version of the file to get current function hashes
            new_fn_hashes: dict[tuple[str | None, str], str] = {}
            if new_disk_path:
                try:
                    from ingestion.parser import parse_file
                    parsed = parse_file(new_disk_path)
                    for fn in parsed.get("functions", []):
                        key = (fn.get("class_name"), fn["name"])
                        if fn.get("source_hash"):
                            new_fn_hashes[key] = fn["source_hash"]
                except Exception:
                    pass

            prev_fn_map: dict[tuple[str | None, str], dict] = {}
            for fn in prev_fns:
                key = (fn.get("class_name"), fn.get("name", ""))
                prev_fn_map[key] = fn

            for key, fn in prev_fn_map.items():
                fn_nid = str(fn["id"])
                rid = fn_nid.split(":", 1)[1] if ":" in fn_nid else fn_nid
                if key not in new_fn_hashes:
                    status = "red"
                elif fn.get("content_hash") == new_fn_hashes[key]:
                    status = "green"
                else:
                    status = "yellow"
                await db.query(
                    "UPDATE type::record('function', $rid) SET diff_status = $s",
                    {"rid": rid, "s": status},
                )
                events.append({"node_id": fn_nid, "status": status, "name": fn.get("name", "")})

        return events

    @staticmethod
    async def run(
        repo_path: str,
        prev_ingestion_id: str,
        db,
        new_snapshot_path: Path | None = None,
        new_ingestion_id: str | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Async generator yielding diff events for each file and function.

        Each event: {"node_id": str, "status": "green"|"yellow"|"red"|"added", "path": str}
          green  = unchanged
          yellow = modified
          red    = deleted (in prev, not in new)
          added  = new file/function (in new, not in prev)

        Uses tar snapshot comparison when snapshots are available, falling back
        to DB hash comparison otherwise.
        """
        prev_iid_bare = (
            prev_ingestion_id.split(":", 1)[1]
            if ":" in prev_ingestion_id
            else prev_ingestion_id
        )
        old_snapshot = SNAPSHOT_DIR / f"{prev_iid_bare}.tar"

        # Collect file events first, then do function-level diff
        file_events: list[dict] = []

        if (
            old_snapshot.exists()
            and new_snapshot_path is not None
            and Path(new_snapshot_path).exists()
        ):
            prev_file_rows = _get_rows(
                await db.query(
                    "SELECT path FROM file WHERE ingestion_id = $iid",
                    {"iid": prev_ingestion_id},
                )
            )
            prev_disk = repo_path
            if prev_file_rows:
                dirs = [str(Path(r["path"]).parent) for r in prev_file_rows]
                try:
                    prev_disk = os.path.commonpath(dirs)
                except ValueError:
                    pass

            prev_rel_to_abs: dict[str, str] = {}
            for row in prev_file_rows:
                abs_path = row["path"]
                try:
                    rel = str(Path(abs_path).relative_to(prev_disk))
                    prev_rel_to_abs[rel] = abs_path
                except ValueError:
                    prev_rel_to_abs[abs_path] = abs_path

            events = diff_snapshots(old_snapshot, Path(new_snapshot_path))
            for event in events:
                rel_path = event["path"]
                abs_path = prev_rel_to_abs.get(
                    rel_path, str(Path(prev_disk) / rel_path)
                )
                node_id = _file_node_id(abs_path, prev_ingestion_id)
                rid = node_id.split(":", 1)[1]
                await db.query(
                    "UPDATE type::record('file', $rid) SET diff_status = $s",
                    {"rid": rid, "s": event["status"]},
                )
                file_event = {"node_id": node_id, "status": event["status"], "path": abs_path, "_rel": rel_path}
                file_events.append(file_event)
                yield {"node_id": node_id, "status": event["status"], "path": abs_path}

        else:
            prev_rows = _get_rows(
                await db.query(
                    "SELECT path, content_hash FROM file WHERE ingestion_id = $iid",
                    {"iid": prev_ingestion_id},
                )
            )
            prev_map: dict[str, str | None] = {
                row["path"]: row.get("content_hash") for row in prev_rows
            }

            current_map: dict[str, str] = {}
            for py_file in Path(repo_path).rglob("*.py"):
                if any(part in _SKIP for part in py_file.parts):
                    continue
                try:
                    current_map[str(py_file)] = content_hash_file(str(py_file))
                except OSError:
                    pass

            prev_paths = set(prev_map.keys())
            current_paths = set(current_map.keys())

            for path in prev_paths & current_paths:
                node_id = _file_node_id(path, prev_ingestion_id)
                rid = node_id.split(":", 1)[1]
                prev_hash = prev_map[path]
                curr_hash = current_map[path]
                status = "green" if prev_hash == curr_hash else "yellow"
                await db.query(
                    "UPDATE type::record('file', $rid) SET diff_status = $s",
                    {"rid": rid, "s": status},
                )
                file_event = {"node_id": node_id, "status": status, "path": path}
                file_events.append(file_event)
                yield file_event

            for path in prev_paths - current_paths:
                node_id = _file_node_id(path, prev_ingestion_id)
                rid = node_id.split(":", 1)[1]
                await db.query(
                    "UPDATE type::record('file', $rid) SET diff_status = $s",
                    {"rid": rid, "s": "red"},
                )
                file_event = {"node_id": node_id, "status": "red", "path": path}
                file_events.append(file_event)
                yield file_event

        # Function-level diff for all files
        for fe in file_events:
            new_disk_path = None
            if fe["status"] == "yellow":
                # Use relative path if available, otherwise derive from abs path
                rel = fe.get("_rel")
                if rel:
                    candidate = str(Path(repo_path) / rel)
                else:
                    try:
                        rel = str(Path(fe["path"]).relative_to(repo_path))
                        candidate = fe["path"]
                    except ValueError:
                        candidate = None
                if candidate and Path(candidate).exists():
                    new_disk_path = candidate

            fn_events = await DiffEngine._diff_functions(
                db, prev_ingestion_id, fe["path"], fe["status"],
                new_disk_path=new_disk_path,
            )
            for fn_ev in fn_events:
                yield fn_ev

        # --- "Added" pass: mark new files/functions not present in previous version ---
        if new_ingestion_id:
            prev_file_rows = _get_rows(await db.query(
                "SELECT path FROM file WHERE ingestion_id = $iid",
                {"iid": prev_ingestion_id},
            ))
            prev_paths = {r["path"] for r in prev_file_rows}

            new_file_rows = _get_rows(await db.query(
                "SELECT id, path FROM file WHERE ingestion_id = $iid",
                {"iid": new_ingestion_id},
            ))

            for nf in new_file_rows:
                if nf["path"] not in prev_paths:
                    nf_nid = str(nf["id"])
                    rid = nf_nid.split(":", 1)[1] if ":" in nf_nid else nf_nid
                    await db.query(
                        "UPDATE type::record('file', $rid) SET diff_status = $s",
                        {"rid": rid, "s": "added"},
                    )
                    yield {"node_id": nf_nid, "status": "added", "path": nf["path"]}

                    # Mark all functions in this new file as "added"
                    new_fns = _get_rows(await db.query(
                        "SELECT id, name FROM `function` WHERE ingestion_id = $iid AND file = type::record('file', $frid)",
                        {"iid": new_ingestion_id, "frid": rid},
                    ))
                    for fn in new_fns:
                        fn_nid = str(fn["id"])
                        fn_rid = fn_nid.split(":", 1)[1] if ":" in fn_nid else fn_nid
                        await db.query(
                            "UPDATE type::record('function', $rid) SET diff_status = $s",
                            {"rid": fn_rid, "s": "added"},
                        )
                        yield {"node_id": fn_nid, "status": "added", "name": fn.get("name", "")}
