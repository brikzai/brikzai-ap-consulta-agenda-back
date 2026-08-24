# agenda-service — Plan 04: Authentication (JWT + CERC Token Provider) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the app the two authentication primitives every later view/CERC call needs — `shared/jwt_auth.py` (validates the corporate IdP's Bearer JWT, resolves `financiador_id`) and `services/cerc/token_provider.py` (per-tenant CERC OAuth2 client-credentials token, cached and single-flighted) — both copied verbatim from `ap-back-optin`'s current versions.

**Architecture:** Two independent modules (neither imports the other) bundled into one task because both are small, both are pure "copy from `ap-back-optin`" work, and both are prerequisites for Plan 06 (CERC client) — `jwt_auth.py` protects the internal API views, `token_provider.py` is what the CERC client calls before every request. `token_provider.py` depends on `shared.tenant_config` (Plan 03, already merged) for per-tenant `cerc_client_id`/`cerc_client_secret`.

**Tech Stack:** `pyjwt[crypto]`, `httpx` (all already in `requirements.txt`). Tests use `cryptography` (pulled in transitively by `pyjwt[crypto]`) to generate a throwaway RSA keypair, and `respx` to mock the CERC OAuth endpoint — no real network calls, no real IdP needed.

**Spec:** `docs/superpowers/specs/2026-08-24-agenda-service-design.md` (§3.3, §3.4, §8). Series: plan 4 of ~10.

**Depends on:** `2026-08-24-agenda-plan-03-cloudsql-client.md` (`shared/tenant_config.get_tenant_config`, which `token_provider.py` calls for CERC credentials).

## Global Constraints

- These files are copied, not shared as a package, between sibling services — no dependency on `ap-back-optin`/`ap-back-contratos` repos.
- `financiador_id` must be a 14-digit string (CNPJ) — enforced by `jwt_auth.py`'s regex check, not by the caller.
- No hardcoded IdP public key or CERC credentials anywhere in code — `IAM_JWT_PUBLIC_KEY`/`IAM_JWT_ISSUER` come from env vars (via `shared.secrets`-style access, but note `jwt_auth.py` reads them directly with `os.environ`, matching `ap-back-optin`'s exact pattern — not routed through `get_secret`, since the public key/issuer are non-sensitive config, not a secret needing Secret Manager rotation).
- Every DB/CERC-token-provider call in later plans passes `financiador_id` as the first argument — this plan's `token_provider.get_cerc_token(financiador_id)` signature is what Plan 06 calls.

---

### Task 1: `shared/jwt_auth.py` + `services/cerc/token_provider.py`

**Files:**
- Create: `shared/jwt_auth.py`
- Create: `shared/tests/test_jwt_auth.py`
- Create: `services/__init__.py`
- Create: `services/cerc/__init__.py`
- Create: `services/cerc/token_provider.py`
- Create: `services/cerc/tests/__init__.py`
- Create: `services/cerc/tests/test_token_provider.py`

**Interfaces:**
- Consumes: `shared.tenant_config.get_tenant_config(financiador_id)` (Plan 03) — `token_provider.py` reads `cerc_client_id`/`cerc_client_secret` from it.
- Produces: `shared.jwt_auth.jwt_required` (decorator; populates `request.jwt_claims` and `request.financiador_id`), `shared.jwt_auth.validar_bearer_token(header: str) -> dict`, `shared.jwt_auth.JwtAuthError`. `services.cerc.token_provider.get_cerc_token(financiador_id: str) -> str`, `.invalidate_token(financiador_id: str) -> None`. Every later plan's views use `@jwt_required`; Plan 06's CERC client calls `get_cerc_token(request.financiador_id)` before every CERC request and `invalidate_token(...)` on a `401`.

- [ ] **Step 1: Write `shared/jwt_auth.py`**

```python
"""Autenticação Bearer JWT do IdP corporativo (design doc §3.3).

Chave pública RS256 fixa (IAM_JWT_PUBLIC_KEY) e emissor esperado
(IAM_JWT_ISSUER) — sem JWKS/rede, mesmo padrão de shared/secrets.py para
segredos estáticos. Rotas isentas (health, push do Pub/Sub) simplesmente
não usam @jwt_required — não há middleware global com exceção por path.

Multi-tenancy: exige o claim `financiador_id` (CNPJ, 14 dígitos) em todo
JWT válido e o expõe em `request.financiador_id`, além de
`request.jwt_claims`.
"""
import functools
import os
import re

import jwt
from django.http import JsonResponse


class JwtAuthError(Exception):
    def __init__(self, mensagem: str):
        self.mensagem = mensagem
        super().__init__(mensagem)


def _public_key() -> str:
    return os.environ["IAM_JWT_PUBLIC_KEY"].replace("\\n", "\n")


def validar_bearer_token(authorization_header: str) -> dict:
    if not authorization_header or not authorization_header.startswith("Bearer "):
        raise JwtAuthError("header Authorization ausente ou sem esquema Bearer")

    token = authorization_header[len("Bearer "):].strip()
    if not token:
        raise JwtAuthError("token vazio")

    try:
        return jwt.decode(
            token,
            _public_key(),
            algorithms=["RS256"],
            issuer=os.environ["IAM_JWT_ISSUER"],
            options={"require": ["exp", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        raise JwtAuthError("token expirado")
    except jwt.InvalidTokenError as exc:
        raise JwtAuthError(f"token inválido: {exc}")


def jwt_required(view_func):
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        try:
            claims = validar_bearer_token(request.headers.get("Authorization", ""))
        except JwtAuthError as exc:
            return JsonResponse({"erro": "NAO_AUTENTICADO", "mensagem": exc.mensagem}, status=401)

        financiador_id = claims.get("financiador_id")
        if not financiador_id or not re.fullmatch(r"\d{14}", str(financiador_id)):
            return JsonResponse(
                {"erro": "NAO_AUTENTICADO", "mensagem": "claim financiador_id ausente ou inválido"}, status=401
            )

        request.jwt_claims = claims
        request.financiador_id = financiador_id
        return view_func(request, *args, **kwargs)

    return wrapper
```

- [ ] **Step 2: Write `shared/tests/test_jwt_auth.py`**

```python
import json
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.http import JsonResponse
from django.test import RequestFactory


@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


@pytest.fixture(autouse=True)
def _set_env(monkeypatch, keypair):
    _, public_pem = keypair
    monkeypatch.setenv("IAM_JWT_PUBLIC_KEY", public_pem)
    monkeypatch.setenv("IAM_JWT_ISSUER", "brikz-iam")


def _token(private_pem, **overrides):
    payload = {
        "exp": int(time.time()) + 300,
        "iss": "brikz-iam",
        "sub": "user-1",
        "financiador_id": "12345678000199",
    }
    payload.update(overrides)
    return pyjwt.encode(payload, private_pem, algorithm="RS256")


def test_validar_bearer_token_aceita_token_valido(keypair):
    from shared.jwt_auth import validar_bearer_token

    private_pem, _ = keypair
    claims = validar_bearer_token(f"Bearer {_token(private_pem)}")
    assert claims["sub"] == "user-1"


def test_validar_bearer_token_rejeita_token_expirado(keypair):
    from shared.jwt_auth import JwtAuthError, validar_bearer_token

    private_pem, _ = keypair
    expirado = _token(private_pem, exp=int(time.time()) - 10)
    with pytest.raises(JwtAuthError):
        validar_bearer_token(f"Bearer {expirado}")


def test_validar_bearer_token_rejeita_issuer_incorreto(keypair):
    from shared.jwt_auth import JwtAuthError, validar_bearer_token

    private_pem, _ = keypair
    outro_issuer = _token(private_pem, iss="outro-idp")
    with pytest.raises(JwtAuthError):
        validar_bearer_token(f"Bearer {outro_issuer}")


def test_validar_bearer_token_rejeita_header_ausente():
    from shared.jwt_auth import JwtAuthError, validar_bearer_token

    with pytest.raises(JwtAuthError):
        validar_bearer_token("")


def test_validar_bearer_token_rejeita_sem_esquema_bearer(keypair):
    from shared.jwt_auth import JwtAuthError, validar_bearer_token

    private_pem, _ = keypair
    with pytest.raises(JwtAuthError):
        validar_bearer_token(_token(private_pem))


def test_jwt_required_retorna_401_sem_header():
    from shared.jwt_auth import jwt_required

    @jwt_required
    def view(request):
        return JsonResponse({"ok": True})

    request = RequestFactory().get("/api/v1/agendas/urs")
    response = view(request)
    assert response.status_code == 401


def test_jwt_required_popula_claims_e_financiador_id_quando_valido(keypair):
    from shared.jwt_auth import jwt_required

    private_pem, _ = keypair
    token = _token(private_pem)

    @jwt_required
    def view(request):
        return JsonResponse({"sub": request.jwt_claims["sub"], "financiador_id": request.financiador_id})

    request = RequestFactory().get("/api/v1/agendas/urs", HTTP_AUTHORIZATION=f"Bearer {token}")
    response = view(request)
    assert response.status_code == 200
    assert json.loads(response.content) == {"sub": "user-1", "financiador_id": "12345678000199"}


def test_jwt_required_retorna_401_sem_claim_financiador_id(keypair):
    from shared.jwt_auth import jwt_required

    private_pem, _ = keypair
    token = pyjwt.encode(
        {"exp": int(time.time()) + 300, "iss": "brikz-iam", "sub": "user-1"}, private_pem, algorithm="RS256"
    )

    @jwt_required
    def view(request):
        return JsonResponse({"ok": True})

    request = RequestFactory().get("/api/v1/agendas/urs", HTTP_AUTHORIZATION=f"Bearer {token}")
    response = view(request)
    assert response.status_code == 401


def test_jwt_required_retorna_401_com_financiador_id_mal_formatado(keypair):
    from shared.jwt_auth import jwt_required

    private_pem, _ = keypair
    token = _token(private_pem, financiador_id="abc123")

    @jwt_required
    def view(request):
        return JsonResponse({"ok": True})

    request = RequestFactory().get("/api/v1/agendas/urs", HTTP_AUTHORIZATION=f"Bearer {token}")
    response = view(request)
    assert response.status_code == 401
```

- [ ] **Step 3: Write `services/__init__.py` and `services/cerc/__init__.py` (both empty)**

- [ ] **Step 4: Write `services/cerc/token_provider.py`**

```python
"""OAuth2 client-credentials — obtém e cacheia o access token da CERC, por
tenant (financiador).

Cache em memória por processo, uma entrada por financiador_id. Renovação
proativa a 80% de expires_in (uma chamada depois desse ponto sempre busca
um token novo, nunca devolve um perto de vencer). Single-flight por tenant
via threading.Lock com double-checked locking: o caminho comum (token em
cache, ainda válido) nunca bloqueia; só quem chega com o cache frio/vencido
disputa o lock daquele tenant, e só um deles de fato faz a chamada HTTP.

client_id/client_secret vêm de shared.tenant_config.get_tenant_config —
CERC_AUTH_URL continua env var global (host do ambiente CERC, não varia
por tenant). Ver design doc §3.4/§8.

Em 401 numa chamada à API da CERC, quem fez a chamada (services/cerc/client.py,
Plano 06) invalida o cache daquele tenant com invalidate_token(financiador_id)
e tenta de novo uma única vez — o retry em si não é responsabilidade deste
módulo.
"""

import os
import threading
import time

import httpx

from shared.tenant_config import get_tenant_config

_meta_lock = threading.Lock()
_locks: dict = {}
_caches: dict = {}


def _lock_for(financiador_id: str) -> threading.Lock:
    if financiador_id not in _locks:
        with _meta_lock:
            if financiador_id not in _locks:
                _locks[financiador_id] = threading.Lock()
    return _locks[financiador_id]


def _fetch_token(financiador_id: str) -> dict:
    config = get_tenant_config(financiador_id)
    response = httpx.post(
        os.environ["CERC_AUTH_URL"],
        data={
            "grant_type": "client_credentials",
            "client_id": config["cerc_client_id"],
            "client_secret": config["cerc_client_secret"],
        },
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()


def get_cerc_token(financiador_id: str) -> str:
    now = time.time()
    cache = _caches.get(financiador_id)
    if cache and cache["access_token"] and now < cache["expires_at"]:
        return cache["access_token"]

    with _lock_for(financiador_id):
        now = time.time()
        cache = _caches.get(financiador_id)
        if cache and cache["access_token"] and now < cache["expires_at"]:
            return cache["access_token"]

        payload = _fetch_token(financiador_id)
        _caches[financiador_id] = {
            "access_token": payload["access_token"],
            "expires_at": now + 0.8 * payload["expires_in"],
        }
        return _caches[financiador_id]["access_token"]


def invalidate_token(financiador_id: str) -> None:
    with _lock_for(financiador_id):
        _caches.pop(financiador_id, None)
```

- [ ] **Step 5: Write `services/cerc/tests/__init__.py` (empty) and `services/cerc/tests/test_token_provider.py`**

```python
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
```

Note: this test file monkeypatches `TENANT_12345678000199_CONFIG` to a **CERC-only** JSON (no `cloudsql_*` keys) via `monkeypatch.setenv`, which shadows the real `.env` value for the duration of each test function only — `monkeypatch` restores the original afterward. This does not corrupt the real dev-tenant config `shared/tests/test_cloudsql_client.py` relies on, for the same reason already established in Plan 03.

- [ ] **Step 6: Run the full test suite**

Run: `pytest shared/tests/test_jwt_auth.py services/cerc/tests/test_token_provider.py -v`
Expected: PASS on all tests — none of these hit the real database or a real network endpoint (JWT tests use a locally-generated throwaway RSA keypair; token provider tests use `respx` to mock the CERC OAuth endpoint), so this should run in well under 5 seconds, unlike Plan 03's suite.

Then run: `pytest -v` (full suite) to confirm nothing in Plans 01-03 broke.

- [ ] **Step 7: Commit**

```bash
git add shared/jwt_auth.py shared/tests/test_jwt_auth.py services/
git commit -m "feat: JWT auth + CERC token provider (per-tenant), copied from ap-back-optin"
```

---

## Self-Review Notes

- **Spec coverage:** design doc §3.3 (JWT validation, `financiador_id` claim, `request.financiador_id`), §3.4 (per-tenant CERC token, single-flight, 80% renewal), §8 step 2 (token provider usage in the future CERC client) → fully covered for what this plan owns.
- **Placeholder scan:** none — every step has runnable code, copied verbatim from `ap-back-optin`'s current files (controller re-read them immediately before writing this plan, unchanged since Plan 03).
- **Type consistency:** `jwt_required` decorator populates `request.jwt_claims: dict` and `request.financiador_id: str`, matching exactly what Plan 09 (API views) and Plan 07 (webhook receiver, if it needs auth) will read. `get_cerc_token(financiador_id: str) -> str` and `invalidate_token(financiador_id: str) -> None` are the exact names/signatures Plan 06's `services/cerc/client.py` will call.
- **Known scope boundary:** this plan does not implement `services/cerc/client.py` (the actual `consultar_agenda` CERC call) — that's Plan 06, which depends on this plan's `get_cerc_token`.

**Next:** `2026-08-24-agenda-plan-05-upsert-repository.md` (`agenda_ur`/`agenda_ur_pagamento` upsert-by-frescor repository, design doc §8/§15 risk 1 — decides the conditional-upsert-vs-Python-compare question left open by Plan 03).
