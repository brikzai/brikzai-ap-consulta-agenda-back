import json
import threading

import httpx
import pytest
import respx

from services.cerc import token_provider

FINANCIADOR_TESTE = "12345678000199"


@pytest.fixture(autouse=True)
def _reset_cache_and_env(monkeypatch):
    monkeypatch.setenv("CERC_AUTH_URL", "https://api.int.cerc.com/oauth/token")
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv(f"TENANT_{FINANCIADOR_TESTE}_CONFIG", json.dumps({
        "cerc_client_id": "client-123",
        "cerc_client_secret": "segredo-local",
    }))
    token_provider._caches.clear()
    token_provider._locks.clear()

    import shared.tenant_config as tenant_config_module
    tenant_config_module._cache.clear()
    yield
    tenant_config_module._cache.clear()


@respx.mock
def test_get_cerc_token_fetches_and_caches():
    route = respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )

    token = token_provider.get_cerc_token(FINANCIADOR_TESTE)
    assert token == "tok-1"
    assert route.call_count == 1

    token_again = token_provider.get_cerc_token(FINANCIADOR_TESTE)
    assert token_again == "tok-1"
    assert route.call_count == 1  # cached, no second call


@respx.mock
def test_get_cerc_token_refetches_after_80_percent_expiry():
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    token_provider.get_cerc_token(FINANCIADOR_TESTE)

    route = respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-2", "expires_in": 3600})
    )
    calls_before = route.call_count
    token_provider._caches[FINANCIADOR_TESTE]["expires_at"] = 0.0  # simula 80% de expires_in decorrido

    token = token_provider.get_cerc_token(FINANCIADOR_TESTE)
    assert token == "tok-2"
    assert route.call_count == calls_before + 1


@respx.mock
def test_get_cerc_token_single_flight_under_concurrency():
    route = respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )

    results = []

    def _call():
        results.append(token_provider.get_cerc_token(FINANCIADOR_TESTE))

    threads = [threading.Thread(target=_call) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == ["tok-1"] * 10
    assert route.call_count == 1


@respx.mock
def test_invalidate_token_forces_refetch():
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    token_provider.get_cerc_token(FINANCIADOR_TESTE)

    token_provider.invalidate_token(FINANCIADOR_TESTE)

    route = respx.post("https://api.int.cerc.com/oauth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok-2", "expires_in": 3600})
    )
    calls_before = route.call_count
    assert token_provider.get_cerc_token(FINANCIADOR_TESTE) == "tok-2"
    assert route.call_count == calls_before + 1


@respx.mock
def test_get_cerc_token_isola_cache_entre_tenants(monkeypatch):
    monkeypatch.setenv("TENANT_99999999000191_CONFIG", json.dumps({
        "cerc_client_id": "client-999",
        "cerc_client_secret": "outro-segredo",
    }))
    respx.post("https://api.int.cerc.com/oauth/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-tenant-1", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok-tenant-2", "expires_in": 3600}),
        ]
    )

    token1 = token_provider.get_cerc_token(FINANCIADOR_TESTE)
    token2 = token_provider.get_cerc_token("99999999000191")

    assert token1 == "tok-tenant-1"
    assert token2 == "tok-tenant-2"
