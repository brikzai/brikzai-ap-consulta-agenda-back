import base64
import hmac
import json
import logging
from datetime import datetime

from django.http import JsonResponse
from django.views.decorators.http import require_POST
from sqlalchemy.exc import DBAPIError
from ulid import ULID

from apps.agenda.webhook_dedupe import hash_evento
from shared import pubsub_client
from shared.cloudsql_client import get_db
from shared.tenant_config import get_tenant_config

logger = logging.getLogger(__name__)


def health(request):
    return JsonResponse({"status": "ok"})


def _violacao_unique(erro: DBAPIError) -> bool:
    """True se o DBAPIError é uma violação de constraint UNIQUE (sqlstate
    23505) — pg8000 não popula os subtipos de exceção da PEP 249, então
    toda falha do protocolo chega como DatabaseError genérico; o único
    jeito confiável de distinguir "duplicado" de "outro erro de banco" é
    inspecionar o dicionário de campos da mensagem de erro do Postgres."""
    args = getattr(erro.orig, "args", None)
    return bool(args) and isinstance(args[0], dict) and args[0].get("C") == "23505"


def _autenticado(request, financiador_id: str) -> bool:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Basic "):
        return False
    try:
        decodificado = base64.b64decode(header[len("Basic "):]).decode("utf-8")
    except Exception:
        return False
    usuario, _, senha = decodificado.partition(":")

    try:
        config = get_tenant_config(financiador_id)
    except Exception:
        return False

    usuario_esperado = config.get("webhook_basic_user")
    senha_esperada = config.get("webhook_basic_password")
    if not usuario_esperado or not senha_esperada:
        logger.error("[Webhook] Credenciais Basic não configuradas para o tenant %s", financiador_id)
        return False

    ok_usuario = hmac.compare_digest(usuario.encode("utf-8"), usuario_esperado.encode("utf-8"))
    ok_senha = hmac.compare_digest(senha.encode("utf-8"), senha_esperada.encode("utf-8"))
    return ok_usuario and ok_senha


@require_POST
def webhook_agenda(request, financiador_id: str):
    """Receptor do webhook CERC (tipoEvento=agenda) — SPEC03 §5.2/§5.3.

    Fino por design: autentica, grava em webhook_inbox, publica no Pub/Sub
    e responde. Nenhuma correlação/upsert acontece aqui — isso é do
    consumidor da push subscription (processar_webhook_agenda)."""
    if not _autenticado(request, financiador_id):
        return JsonResponse({"erro": "autenticação inválida"}, status=401)

    try:
        envelope = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"erro": "corpo não é JSON válido"}, status=400)

    tipo_evento = envelope.get("tipoEvento") if isinstance(envelope, dict) else None
    data_hora_evento = envelope.get("dataHoraEvento") if isinstance(envelope, dict) else None
    evento = envelope.get("evento") if isinstance(envelope, dict) else None
    if not tipo_evento or not data_hora_evento or evento is None:
        return JsonResponse(
            {"erro": "envelope inválido: tipoEvento, dataHoraEvento e evento são obrigatórios"}, status=400,
        )

    hash_dedupe = hash_evento(tipo_evento, evento, data_hora_evento)
    webhook_id = str(ULID())

    try:
        get_db(financiador_id).table("webhook_inbox").insert({
            "id": webhook_id,
            "tipo_evento": tipo_evento,
            "data_hora_evento": datetime.fromisoformat(data_hora_evento),
            "payload": envelope,
            "hash_dedupe": hash_dedupe,
        }).execute()
    except DBAPIError as erro:
        if _violacao_unique(erro):
            logger.info("[Webhook] Evento duplicado ignorado (financiador=%s, hash=%s)", financiador_id, hash_dedupe)
            return JsonResponse({}, status=202)
        logger.exception("[Webhook] Falha ao persistir webhook_inbox (financiador=%s)", financiador_id)
        return JsonResponse({"erro": "falha ao persistir evento"}, status=500)
    except Exception:
        logger.exception("[Webhook] Falha ao persistir webhook_inbox (financiador=%s)", financiador_id)
        return JsonResponse({"erro": "falha ao persistir evento"}, status=500)

    try:
        pubsub_client.publish_webhook_agenda(webhook_id, financiador_id)
    except Exception:
        logger.exception("[Webhook] publish_webhook_agenda levantou inesperadamente (financiador=%s)", financiador_id)

    return JsonResponse({}, status=202)
