import hashlib
import tarfile
from pathlib import Path

SNAPSHOT_DIR = Path.home() / ".dead-reckoning" / "snapshots"
_SKIP = {"__pycache__", ".git", "venv", ".venv", "node_modules", ".tox"}


def _sha256_member(tf: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    f = tf.extractfile(member)
    h = hashlib.sha256()
    for chunk in iter(lambda: f.read(8192), b""):
        h.update(chunk)
    return h.hexdigest()


def create_snapshot(disk_path: str, ingestion_id: str) -> Path:
    """Tar all .py files into ~/.dead-reckoning/snapshots/{ingestion_id}.tar"""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    out = SNAPSHOT_DIR / f"{ingestion_id}.tar"
    root = Path(disk_path)
    with tarfile.open(out, "w") as tf:
        for py in sorted(root.rglob("*.py")):
            if any(p in _SKIP for p in py.parts):
                continue
            tf.add(py, arcname=str(py.relative_to(root)))
    return out


def read_snapshot(tar_path: Path) -> dict[str, str]:
    """Returns {relative_path: sha256} for all members."""
    result = {}
    with tarfile.open(tar_path, "r") as tf:
        for m in tf.getmembers():
            if m.isfile():
                result[m.name] = _sha256_member(tf, m)
    return result


def diff_snapshots(old_tar: Path, new_tar: Path) -> list[dict]:
    """
    Returns list of {path, status} where status is green/yellow/red.
    path values are relative to the repo root (as stored in the tar).
    New files (absent from old tar) are omitted — no prev node to colour.
    """
    old = read_snapshot(old_tar)
    new = read_snapshot(new_tar)
    events = []
    for path, old_hash in old.items():
        if path not in new:
            events.append({"path": path, "status": "red"})
        elif new[path] == old_hash:
            events.append({"path": path, "status": "green"})
        else:
            events.append({"path": path, "status": "yellow"})
    return events
