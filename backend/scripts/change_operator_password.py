"""Rotate the application password and sessions without changing infrastructure secrets."""
from __future__ import annotations

import argparse
import getpass
import io
import os
from pathlib import Path
import re
import secrets
import tempfile

from argon2 import PasswordHasher
from dotenv import dotenv_values


def rotate(path: Path, password: str) -> None:
    if not 15 <= len(password) <= 512:
        raise ValueError("Use a new application password of 15-512 characters")
    if path.is_symlink() or not path.is_file():
        raise ValueError("Select an existing regular private environment file")
    source = path.read_text(encoding="utf-8")
    original = source
    values = dotenv_values(stream=io.StringIO(source))
    previous = values.get("OPERATOR_PASSWORD_HASH")
    if previous:
        from argon2.exceptions import VerificationError, InvalidHashError
        try:
            if PasswordHasher().verify(previous, password):
                raise ValueError("Choose a different application password")
        except (VerificationError, InvalidHashError):
            pass
    replacements = {
        "OPERATOR_PASSWORD_HASH": PasswordHasher().hash(password),
        "SESSION_SECRET": secrets.token_urlsafe(48),
    }
    for key, value in replacements.items():
        pattern = re.compile(r"^" + key + r"=.*$", re.M)
        if len(pattern.findall(original)) != 1:
            raise ValueError(f"Expected exactly one {key} entry")
        original = pattern.sub(lambda _: f"{key}='{value}'", original)
    descriptor, name = tempfile.mkstemp(prefix=".env-rotate-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_text(encoding="utf-8") != source:
            raise ValueError("Environment file changed concurrently")
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    password = getpass.getpass("New application password (different from SSH): ")
    if password != getpass.getpass("Repeat new application password: "):
        parser.error("Passwords do not match")
    try:
        rotate(args.env_file, password)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print("Application password and session signing key rotated. Infrastructure secrets preserved.")
    print("Recreate API and worker containers to load the new configuration.")


if __name__ == "__main__":
    main()
