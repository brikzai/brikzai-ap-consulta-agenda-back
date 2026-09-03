"""Configuração por tenant (financiador) — multi-tenancy (design doc §3.2).

Um segredo por tenant (AGENDA_TENANT_{financiador_id}_CONFIG, JSON) via
shared.secrets.get_secret — dev local lê a env var de mesmo nome (sem
GOOGLE_CLOUD_PROJECT); produção/homolog lê do Secret Manager, um segredo
por tenant. Cacheado em memória por processo, sem TTL (mesma filosofia do
cache de token de services/cerc/token_provider.py, Plano 04).

Prefixo "AGENDA_" (em vez de só "TENANT_{cnpj}_CONFIG", como em
ap-back-optin/ap-back-contratos): quando este serviço roda no mesmo
projeto GCP dos irmãos, os três leem segredos do mesmo Secret Manager —
sem o prefixo, o nome colidiria com o segredo de outro serviço para o
mesmo tenant (docs/runbooks/gcp-setup.md).
"""
import json

from shared.secrets import get_secret

_cache: dict = {}


def get_tenant_config(financiador_id: str) -> dict:
    if financiador_id in _cache:
        return _cache[financiador_id]

    raw = get_secret(f"AGENDA_TENANT_{financiador_id}_CONFIG")
    config = json.loads(raw)
    _cache[financiador_id] = config
    return config
