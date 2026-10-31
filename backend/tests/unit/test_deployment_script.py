"""Exercise update success, health rollback and schema refusal without Docker."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="Linux Bash deployment contract")


@pytest.mark.parametrize("scenario", ["success", "health", "schema"])
def test_server_update_preserves_secrets_and_recovers_failed_health(tmp_path, scenario):
    source = Path(__file__).resolve().parents[3] / "ops/update-server.sh"
    app, config, bindir = tmp_path / "app", tmp_path / "private", tmp_path / "bin"
    (app / "ops").mkdir(parents=True)
    (app / ".git").mkdir()
    config.mkdir()
    bindir.mkdir()
    script = app / "ops/update-server.sh"
    script.write_bytes(source.read_bytes())
    (config / ".env").write_text("SECRET=synthetic-only\n", encoding="utf-8")
    (config / "compose.server-test.yml").write_text("services: {}\n", encoding="utf-8")
    mockgit = '#!/usr/bin/env bash\nif [[ "$*" == *"status --porcelain"* ]]; then exit 0; fi\nif [[ "$1" == rev-parse && "$2" == HEAD ]]; then printf \'%040d\\n\' 1; exit 0; fi\nif [[ "$1" == rev-parse && "$2" == --verify ]]; then printf \'%040d\\n\' 2; exit 0; fi\nif [[ "$1" == rev-parse && "$2" == --short=12 ]]; then echo 000000000002; exit 0; fi\nif [[ "$1" == diff && "$MOCK_SCENARIO" == schema ]]; then exit 1; fi\nexit 0\n'
    mockdocker = '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$MOCK_LOG"\nif [[ "$1" == ps ]]; then echo aetherrag-app:previous; fi\nif [[ "$1" == compose && "$*" == *" up "* && "$MOCK_SCENARIO" == health ]]; then\n  if grep -q git-000000000002 "$MOCK_OVERLAY"; then exit 7; fi\nfi\nexit 0\n'
    for name, text in {"git": mockgit, "docker": mockdocker, "flock": "#!/usr/bin/env bash\nexit 0\n"}.items():
        path = bindir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        path.chmod(0o755)
    log = tmp_path / "commands"
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"],
               MOCK_LOG=str(log), MOCK_SCENARIO=scenario,
               MOCK_OVERLAY=str(config / ".deploy-state/image.yml"))
    result = subprocess.run(["bash", str(script), str(config), "aetherrag-test"], env=env, capture_output=True)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert (config / ".env").read_text(encoding="utf-8") == "SECRET=synthetic-only\n"
    if scenario == "success":
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert (config / ".deploy-state/history").exists()
        assert any("exec -T api" in call for call in calls)
    elif scenario == "health":
        assert result.returncode == 7
        assert (config / ".deploy-state/image.yml").read_text().count("aetherrag-app:previous") == 4
        assert sum(" up " in call for call in calls) == 2
    else:
        assert result.returncode == 2
        assert not calls
    assert not any(" down " in call for call in calls)
