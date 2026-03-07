import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path


def is_github_url(s: str) -> bool:
    s = s.strip()
    return s.startswith("https://github.com/") or s.startswith("git@github.com:")


def clone_repo(url: str) -> tuple[str, callable]:
    """Clone a GitHub repo at depth 1 into a temp dir.

    Returns (local_path, cleanup_fn). Call cleanup_fn() when done.
    """
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    tmp_dir = tempfile.mkdtemp(prefix=f"dr_{url_hash}_")
    dest = Path(tmp_dir) / "repo"

    result = subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise RuntimeError(f"git clone failed: {result.stderr.strip()}")

    def cleanup():
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return str(dest), cleanup
