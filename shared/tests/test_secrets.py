from shared.secrets import get_secret
import pytest


def test_get_secret_reads_env_var_when_no_gcp_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("MY_SECRET", "valor-local")
    assert get_secret("MY_SECRET") == "valor-local"


def test_get_secret_raises_when_missing_locally(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("NAO_EXISTE", raising=False)
    with pytest.raises(RuntimeError):
        get_secret("NAO_EXISTE")
