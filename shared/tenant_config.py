"""Configuração por tenant (financiador) — multi-tenancy (design doc §3.2).

Um segredo por tenant (TENANT_{financiador_id}_CONFIG, JSON) via
shared.secrets.get_secret — dev local lê a env var de mesmo nome (sem
GOOGLE_CLOUD_PROJECT); produção/homolog lê do Secret Manager, um segredo
por tenant. Cacheado em memória por processo, sem TTL (mesma filosofia do
cache de token de services/cerc/token_provider.py, Plano 04).
"""
import json

from shared.secrets import get_secret

_cache: dict = {}


def get_tenant_config(financiador_id: str) -> dict:
    if financiador_id in _cache:
        return _cache[financiador_id]

    raw = get_secret(f"TENANT_{financiador_id}_CONFIG")
    config = json.loads(raw)
    _cache[financiador_id] = config
    return config
