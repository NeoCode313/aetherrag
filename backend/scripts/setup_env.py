"""Create an operator configuration once; never prints generated credentials.

Run after installing requirements.lock. Existing output is never overwritten.
"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import secrets

from argon2 import PasswordHasher


def configuration(username: str, password: str) -> str:
    if not username or not username.isascii() or len(username) > 128 or not all(c.isalnum() or c in "_.-" for c in username):
        raise ValueError("Operator name permits letters, numbers, underscore, dot and dash")
    if len(password) < 15 or len(password) > 512:
        raise ValueError("Use an operator password containing 15-512 characters")
    database_password, redis_password = secrets.token_hex(24), secrets.token_hex(24)
    values = {
        "ENVIRONMENT": "production", "DATA_DIR": "./data", "OPERATOR_USERNAME": username,
        "OPERATOR_PASSWORD_HASH": PasswordHasher().hash(password),
        "SESSION_SECRET": secrets.token_urlsafe(48), "SESSION_SECURE": "true", "AUTH_ENABLED": "true",
        "DB_PASSWORD": database_password,
        "DATABASE_URL": f"postgresql+psycopg://aetherrag:{database_password}@postgres:5432/aetherrag",
        "REDIS_PASSWORD": redis_password, "REDIS_URL": f"redis://:{redis_password}@redis:6379/0",
        "QDRANT_API_KEY": secrets.token_urlsafe(32), "QDRANT_URL": "http://qdrant:6333",
        "QUEUE_BACKEND": "celery", "STATE_BACKEND": "postgres", "VECTOR_BACKEND": "qdrant",
        "AUTO_SEED_SAMPLE_MANUAL": "false", "LLM_PROVIDER": "local", "EMBEDDING_PROVIDER": "local",
        "EMBEDDING_DIMENSION": "768", "PIPELINE_VERSION": "layout-v3",
        "GEMINI_API_KEY": "", "OPENAI_API_KEY": "", "LLM_SUPPORTS_IMAGES": "false",
    }
    # Compose dotenv single quotes preserve dollar signs in Argon2 hashes.
    return "# Generated private operator configuration. Never commit.\n" + "".join(
        f"{key}='{value}'\n" for key, value in values.items())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".env"))
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-env", help="Read the password from this environment variable instead of prompting")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; rotate credentials explicitly instead of overwriting")
    if args.password_env:
        password = os.environ.get(args.password_env, "")
    else:
        password = getpass.getpass("Operator password (at least 15 characters): ")
        if password != getpass.getpass("Repeat password: "):
            parser.error("Passwords do not match")
    try:
        content = configuration(args.username, password)
    except ValueError as exc:
        parser.error(str(exc))
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
    print(f"Configuration created: {args.output.resolve()}")
    print("Start: docker compose up --build -d")
    print("Open https://localhost:8443; trust the local Caddy CA before using the operator session.")


if __name__ == "__main__":
    main()
