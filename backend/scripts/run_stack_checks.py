"""Run the opt-in real Compose checks without deleting application volumes.

Requires a working Docker daemon and a configuration created by setup_env.py.
The operator password is read privately, passed only to the test process and never
written to .env. This checks infrastructure with local models, not cloud vision.
"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--no-start", action="store_true", help="Test the stack already running")
    parser.add_argument("--password-env", help="Read the operator password from this environment variable")
    args = parser.parse_args()
    if not args.env_file.is_file():
        parser.error("Create the private environment configuration with setup_env.py first")
    password = os.environ.get(args.password_env, "") if args.password_env else getpass.getpass("Operator password: ")
    if not password:
        parser.error("The real authenticated stack check requires the operator password")
    env = dict(os.environ)
    env["STACK_TEST_PASSWORD"] = password
    env["AETHERRAG_ENV_FILE"] = str(args.env_file.resolve())
    prefix = ["docker", "compose", "--env-file", str(args.env_file.resolve())]
    subprocess.run(prefix + ["config", "--quiet"], env=env, check=True)
    if not args.no_start:
        subprocess.run(prefix + ["up", "--build", "-d"], env=env, check=True)
    subprocess.run(prefix + ["--profile", "test", "run", "--rm", "integration-tests"], env=env, check=True)
    print("Real stack checks passed. Existing application volumes were retained.")


if __name__ == "__main__":
    main()
