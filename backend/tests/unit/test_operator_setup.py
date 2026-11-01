"""The operator bootstrap preserves hashes and never embeds plaintext passwords."""
import io
from pathlib import Path
import sys

from argon2 import PasswordHasher
from dotenv import dotenv_values
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from setup_env import configuration


def test_operator_environment_preserves_argon2_and_uses_independent_random_secrets():
    password = "OnlyForThisTest-381928!"
    text = configuration("operator", password)
    values = dotenv_values(stream=io.StringIO(text))
    assert password not in text
    assert PasswordHasher().verify(values["OPERATOR_PASSWORD_HASH"], password)
    assert values["SESSION_SECURE"] == "true"
    assert values["AUTO_SEED_SAMPLE_MANUAL"] == "false"
    assert values["LLM_PROVIDER"] == "local"
    assert len({values["DB_PASSWORD"], values["REDIS_PASSWORD"], values["QDRANT_API_KEY"], values["SESSION_SECRET"]}) == 4
    assert values["DATABASE_URL"].startswith("postgresql+psycopg://")


@pytest.mark.parametrize("username,password", [("bad\nname", "LongEnoughPassword-21"), ("administración", "LongEnoughPassword-21"), ("admin", "short")])
def test_operator_bootstrap_rejects_unsafe_inputs(username, password):
    with pytest.raises(ValueError):
        configuration(username, password)

def test_development_does_not_use_shared_default_secrets_or_password(tmp_path):
    from app.infrastructure.config import Settings
    from app.presentation.dependencies import AppContainer
    first=AppContainer(Settings(_env_file=None,environment="development",data_dir=tmp_path/"one",
        llm_provider="local",embedding_provider="local",vector_backend="memory",
        auto_seed_sample_manual=False))
    second=AppContainer(Settings(_env_file=None,environment="development",data_dir=tmp_path/"two",
        llm_provider="local",embedding_provider="local",vector_backend="memory",
        auto_seed_sample_manual=False))
    try:
        assert first.settings.auth_enabled
        assert first.identity.read().get("setup_token")
        assert first.settings.session_cookie_name != second.settings.session_cookie_name
        assert first.settings.operator_password_hash==""
        assert first.settings.session_secret!=second.settings.session_secret
    finally:
        first.close();second.close()


def test_v3_cannot_write_new_layout_under_old_pipeline_identity(tmp_path):
    from app.domain.exceptions import ValidationError
    from app.infrastructure.config import Settings
    from app.presentation.dependencies import AppContainer
    with pytest.raises(ValidationError,match="PIPELINE_VERSION=layout-v3"):
        AppContainer(Settings(_env_file=None,pipeline_version="layout-v2",data_dir=tmp_path))

def test_password_rotation_preserves_every_other_secret_and_invalidates_sessions(tmp_path):
    from change_operator_password import rotate
    path=tmp_path/".env"
    path.write_text(configuration("admin","Old-App-Password-12345!"),encoding="utf-8")
    before=dotenv_values(path)
    rotate(path,"New-App-Password-98765!")
    after=dotenv_values(path)
    assert PasswordHasher().verify(after["OPERATOR_PASSWORD_HASH"],"New-App-Password-98765!")
    assert after["SESSION_SECRET"]!=before["SESSION_SECRET"]
    assert all(after[key]==value for key,value in before.items()
               if key not in {"OPERATOR_PASSWORD_HASH","SESSION_SECRET"})
    assert "New-App-Password-98765!" not in path.read_text(encoding="utf-8")


def test_rotation_rejects_reuse_without_changing_env(tmp_path):
    from change_operator_password import rotate
    path=tmp_path/".env"
    source=configuration("admin","Existing-App-Password-12345!")
    path.write_text(source,encoding="utf-8")
    with pytest.raises(ValueError,match="different"):
        rotate(path,"Existing-App-Password-12345!")
    assert path.read_text(encoding="utf-8")==source
