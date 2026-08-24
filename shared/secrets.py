"""Leitura de segredos — Secret Manager em produção/homolog, env var em dev local.

Dev local: sem GOOGLE_CLOUD_PROJECT setado, lê a env var com o mesmo nome do
segredo (ex.: CERC_CLIENT_SECRET no .env). Em produção/homolog, lê do Secret
Manager do projeto (versão "latest").
"""

import os


def get_secret(name: str) -> str:
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"Secret '{name}' não configurado (defina a env var localmente)")
        return value

    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{project}/secrets/{name}/versions/latest"
    response = client.access_secret_version(name=path)
    return response.payload.data.decode("utf-8")
