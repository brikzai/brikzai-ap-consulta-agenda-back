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
