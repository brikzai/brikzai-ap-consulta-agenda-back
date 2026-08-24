import json

import pytest

import shared.tenant_config as tenant_config_module


@pytest.fixture(autouse=True)
def _clear_cache():
    tenant_config_module._cache.clear()
    yield
    tenant_config_module._cache.clear()


CONFIG_EXEMPLO = {
    "cloudsql_connection_name": "proj:region:instance",
    "cloudsql_db_user": "app",
    "cloudsql_db_password": "senha",
    "cloudsql_db_name": "app",
    "cerc_client_id": "client-1",
    "cerc_client_secret": "segredo",
    "cerc_cnpj_solicitante": "12345678000199",
}


def test_get_tenant_config_le_e_parseia_json(monkeypatch):
    from shared.tenant_config import get_tenant_config

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("TENANT_12345678000199_CONFIG", json.dumps(CONFIG_EXEMPLO))

    assert get_tenant_config("12345678000199") == CONFIG_EXEMPLO


def test_get_tenant_config_usa_cache_sem_reler_env(monkeypatch):
    from shared.tenant_config import get_tenant_config

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("TENANT_99999999000191_CONFIG", json.dumps(CONFIG_EXEMPLO))

    primeira = get_tenant_config("99999999000191")
    monkeypatch.delenv("TENANT_99999999000191_CONFIG", raising=False)
    segunda = get_tenant_config("99999999000191")

    assert primeira == segunda


def test_get_tenant_config_propaga_erro_quando_segredo_ausente(monkeypatch):
    from shared.tenant_config import get_tenant_config

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("TENANT_00000000000000_CONFIG", raising=False)

    with pytest.raises(RuntimeError):
        get_tenant_config("00000000000000")
