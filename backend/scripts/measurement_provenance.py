"""Non-secret provenance for repeatable local measurements."""
import hashlib
import platform
import subprocess
from pathlib import Path


def measurement_provenance() -> dict:
    root = Path(__file__).resolve().parents[2]
    files = sorted([*root.joinpath("backend/app").rglob("*.py"),
                    *root.joinpath("backend/scripts").glob("*.py")])
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\\0")
    result = subprocess.run(["git","rev-parse","HEAD"],cwd=root,text=True,capture_output=True,check=False)
    return dict(base_commit=result.stdout.strip() if result.returncode==0 else None,
        source_tree_sha256=digest.hexdigest(),source_file_count=len(files),
        requirements_lock_sha256=hashlib.sha256((root/"requirements.lock").read_bytes()).hexdigest(),
        python_version=platform.python_version())
