"""Configure Groq privately without replacing database, session or embedding secrets."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import tempfile


def configure(path: Path, api_key: str, model: str) -> None:
    if not re.fullmatch(r"gsk_[A-Za-z0-9_-]{16,256}", api_key):
        raise ValueError("Use a Groq key starting with gsk_, without quotes or spaces")
    if not re.fullmatch(r"[A-Za-z0-9_./:-]{1,128}", model):
        raise ValueError("Invalid model identifier")
    if path.is_symlink() or not path.is_file():
        raise ValueError("Select an existing regular private .env file")
    source = path.read_text(encoding="utf-8")
    result = source
    updates = {"LLM_PROVIDER": "openai", "GROQ_API_KEY": api_key,
               "OPENAI_BASE_URL": "https://api.groq.com/openai/v1",
               "OPENAI_MODEL": model, "LLM_SUPPORTS_IMAGES": "false"}
    for key, value in updates.items():
        pattern = re.compile(r"^" + key + r"=.*$", re.M)
        matches = len(pattern.findall(result))
        if matches > 1:
            raise ValueError("Duplicate provider entries in environment file")
        line = f"{key}='{value}'"
        result = pattern.sub(lambda _: line, result) if matches else result.rstrip("\n") + "\n" + line + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".env-rotate-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(result)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_text(encoding="utf-8") != source:
            raise ValueError("Environment file changed concurrently")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--model", default="qwen/qwen3.8-27b")
    args = parser.parse_args()
    key = getpass.getpass("Groq API key (hidden; stored only in private .env): ").strip()
    try:
        configure(args.env_file, key, args.model)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print("Groq configured privately. Database, session and embedding configuration preserved.")
    print("Recreate API/worker, then select OpenAI / Compatible and the configured model in the UI.")
    print("A previously selected provider is preserved in the catalog until explicitly changed.")


if __name__ == "__main__":
    main()
