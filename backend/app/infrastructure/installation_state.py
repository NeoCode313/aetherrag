"""Installation-owned state. Never expose this document through HTTP or catalog exports."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import copy
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading

_LOCK = threading.RLock()


class InstallationState:
    def __init__(self, data_dir: Path, legacy_username="", legacy_hash="", session_secret=""):
        self.directory = data_dir / ".private"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "installation.json"
        self.lock_path = self.directory / "state.lock"
        with self.transaction() as state:
            if not state:
                state.update(installation_id=secrets.token_hex(12),
                             session_secret=session_secret or secrets.token_urlsafe(48),
                             revision=0, provider={})
            if legacy_hash and (not state.get("owner") or (
                    state.get("owner_source") == "environment" and state.get("legacy_hash") != legacy_hash)):
                state["owner"] = {"username": legacy_username or "admin", "password_hash": legacy_hash}
                state["owner_source"] = "environment"
                state["legacy_hash"] = legacy_hash
                state["session_secret"] = session_secret or secrets.token_urlsafe(48)
                state["revision"] += 1
                state.pop("setup_token", None)
            if not state.get("owner") and not state.get("setup_token"):
                state["setup_token"] = secrets.token_urlsafe(32)

    @contextmanager
    def transaction(self):
        with _LOCK:
            with self.lock_path.open("a+b") as lock:
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"0"); lock.flush()
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
                    before = json.dumps(state, sort_keys=True)
                    yield state
                    if json.dumps(state, sort_keys=True) != before:
                        descriptor, name = tempfile.mkstemp(dir=self.directory, prefix="state-")
                        try:
                            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                                json.dump(state, stream, ensure_ascii=False)
                                stream.flush(); os.fsync(stream.fileno())
                            os.replace(name, self.path)
                        finally:
                            Path(name).unlink(missing_ok=True)
                finally:
                    lock.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def read(self):
        # Atomic replacement guarantees whole documents; readers need no writer lock.
        # A file signature detects changes made by another API/worker process.
        with _LOCK:
            signature = self.path.stat()
            stamp = (signature.st_mtime_ns, signature.st_size, signature.st_ino)
            if getattr(self, "_cached_stamp", None) != stamp:
                self._cached_state = json.loads(self.path.read_text(encoding="utf-8"))
                self._cached_stamp = stamp
            return copy.deepcopy(self._cached_state)

    def register(self, username, password_hash, token):
        from app.domain.exceptions import ConflictError, ValidationError
        with self.transaction() as state:
            if state.get("owner"):
                raise ConflictError("El propietario ya existe. Inicia sesión.")
            if not isinstance(token, str) or not secrets.compare_digest(token, state.get("setup_token", "")):
                raise ValidationError("Código de instalación inválido")
            state["owner"] = {"username": username, "password_hash": password_hash}
            state["owner_source"] = "registration"
            state.pop("setup_token", None)
            state["revision"] += 1
            return state["owner"]

    def rotate_password(self, password_hash):
        from app.domain.exceptions import ConflictError
        with self.transaction() as state:
            if not state.get("owner"):
                raise ConflictError("Primero crea el propietario")
            state["owner"]["password_hash"] = password_hash
            state["session_secret"] = secrets.token_urlsafe(48)
            state["revision"] += 1

    def save_provider(self, values):
        with self.transaction() as state:
            state["provider"] = values
            state["revision"] += 1


def source_build() -> str:
    root = Path(__file__).resolve().parents[3]
    digest = hashlib.sha256()
    for path in sorted((root / "backend/app").rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:12]
