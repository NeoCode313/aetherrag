"""Download immutable public model weights; no credentials or document contents are sent."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys
import tempfile

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.infrastructure.llm.semantic_embedding import MODEL, REVISION, FILES, verify_model


def download(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        for name, (remote, expected) in FILES.items():
            target = directory / name
            if target.is_file():
                with target.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() == expected:
                        continue
            descriptor, temporary = tempfile.mkstemp(prefix="model-", dir=directory)
            try:
                digest = hashlib.sha256()
                size = 0
                with os.fdopen(descriptor, "wb") as output:
                    with client.stream("GET", f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{remote}") as response:
                        response.raise_for_status()
                        for block in response.iter_bytes(1024*1024):
                            size += len(block)
                            if size > 150*1024*1024:
                                raise ValueError("Model file exceeds the pinned size budget")
                            digest.update(block)
                            output.write(block)
                if digest.hexdigest() != expected:
                    raise ValueError("Model checksum mismatch")
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
    verify_model(directory)
    print("Multilingual E5 weights and tokenizer verified.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    if args.directory is None:
        from app.infrastructure.config import get_settings
        args.directory = get_settings().embedding_model_dir
    download(args.directory)
