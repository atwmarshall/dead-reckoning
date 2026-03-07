import hashlib
from pathlib import Path
from typing import AsyncGenerator

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


def _get_rows(result) -> list:
    if isinstance(result, list):
        if result and isinstance(result[0], dict) and "result" in result[0]:
            return result[0].get("result") or []
        return result
    return []


class DiffEngine:
    @staticmethod
    async def run(
        repo_path: str,
        prev_ingestion_id: str,
        db,
    ) -> AsyncGenerator[dict, None]:
        """Async generator yielding diff events for each file.

        Each event: {"node_id": str, "status": "green"|"yellow"|"red", "path": str}
          green  = unchanged
          yellow = modified
          red    = deleted (in prev, not on disk)
        New files (on disk but not in prev) are not yielded.
        """
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
            prev_hash = prev_map[path]
            curr_hash = current_map[path]
            status = "green" if prev_hash == curr_hash else "yellow"
            yield {"node_id": node_id, "status": status, "path": path}

        for path in prev_paths - current_paths:
            node_id = _file_node_id(path, prev_ingestion_id)
            yield {"node_id": node_id, "status": "red", "path": path}
