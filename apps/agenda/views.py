import base64
import hmac
import json
import logging
from datetime import datetime, timezone

from django.http import JsonResponse
from django.views.decorators.http import require_POST
from sqlalchemy.exc import DBAPIError
from ulid import ULID

from apps.agenda.correlacao import encontrar_consultas_candidatas
from apps.agenda.repository import upsert_agenda_ur
from apps.agenda.webhook_dedupe import hash_evento
from shared import pubsub_client
from shared.cloudsql_client import get_db
from shared.pubsub_auth import verificar_push_oidc
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


def _traduzir_pagamento_webhook(pagamento: dict) -> dict:
    return {
        "tipo_informacao_pagamento": str(pagamento["tipoInformacaoPagamento"]),
        "indicador_efeitos_contrato": pagamento.get("indicadorEfeitosContrato"),
        "identificador_cerc_contrato": None,  # coluna 12.16 — só o Plano 08 (arquivo AP005) preenche
        "regras_divisao": pagamento.get("regrasDivisao"),
        "valor_onerado": pagamento.get("valorOnerado"),
        "valor_constituido_efeito": pagamento.get("valorConstituidoEfeito"),
        "valor_a_pagar": pagamento.get("valorAPagar"),
        "beneficiario": pagamento.get("beneficiario"),
        "data_liquidacao_efetiva": pagamento.get("dataLiquidacaoEfetiva"),
        "valor_liquidacao_efetiva": pagamento.get("valorLiquidacaoEfetiva"),
        "motivo_nao_pagamento": pagamento.get("motivoDeNaoPagamento"),
        "domicilio": pagamento.get("domicilioPagamento") or {},
    }


def _traduzir_evento_webhook(evento: dict) -> tuple:
    cabecalho = {
        "entidade_registradora": evento["entidadeRegistradora"],
        "cnpj_credenciadora": evento["instituicaoCredenciadora"],
        "codigo_arranjo": evento["codigoArranjoPagamento"],
        "documento_ufr": evento["documentoUsuarioFinalRecebedor"],
        "documento_titular": evento["documentoTitular"],
        "data_liquidacao": evento["dataLiquidacao"],
        "constituicao": evento["constituicao"],
        "valor_constituido_total": evento["valorConstituidoTotal"],
        "valor_constituido_antecipacao_pre": evento.get("valorConstituidoAntecipacaoPre", 0),
        "valor_bloqueado": evento.get("valorBloqueado", 0),
        "valor_livre": evento.get("valorLivre", 0),
        "valor_total_ur": evento["valorTotalUR"],
        "carteira": evento.get("carteira"),
        "data_hora_ultima_atualizacao": datetime.fromisoformat(evento["dataHoraUltimaAtualizacao"].replace("Z", "+00:00")),
        "origem": "WEBHOOK",
        "origem_arquivo": None,
    }
    pagamentos = [_traduzir_pagamento_webhook(p) for p in evento.get("pagamentos", [])]
    return cabecalho, pagamentos


@require_POST
def processar_webhook_agenda(request):
    """Consumidor da push subscription do Pub/Sub — correlaciona a UR do
    evento (SPEC03 §5.4) e persiste via upsert_agenda_ur (origem=WEBHOOK).
    Verificado por OIDC. Idempotente sob reentrega (at-least-once): o guard
    de webhook_inbox.processado_em evita refazer qualquer escrita."""
    if not verificar_push_oidc(request):
        return JsonResponse({"erro": "OIDC inválido"}, status=401)

    try:
        envelope = json.loads(request.body)
        dados = json.loads(base64.b64decode(envelope["message"]["data"]))
        webhook_inbox_id = dados["webhook_inbox_id"]
        financiador_id = dados["financiador_id"]
    except Exception:
        logger.exception("[Processor] Envelope do Pub/Sub push malformado")
        return JsonResponse({"erro": "envelope inválido"}, status=400)

    db = get_db(financiador_id)
    linhas_inbox = db.table("webhook_inbox").select("*").eq("id", webhook_inbox_id).execute()
    if not linhas_inbox.data:
        logger.error("[Processor] webhook_inbox_id=%s não encontrado (financiador=%s)", webhook_inbox_id, financiador_id)
        return JsonResponse({"erro": "webhook_inbox não encontrado"}, status=404)
    inbox = linhas_inbox.data[0]

    if inbox["processado_em"] is not None:
        return JsonResponse({}, status=204)

    payload = inbox["payload"]
    tipo_evento = payload.get("tipoEvento")
    evento = payload.get("evento")

    if tipo_evento != "agenda":
        logger.warning("[Processor] tipoEvento=%s fora do escopo deste consumidor, ignorando", tipo_evento)
        db.table("webhook_inbox").update({"processado_em": datetime.now(timezone.utc)}).eq("id", webhook_inbox_id).execute()
        return JsonResponse({}, status=204)

    try:
        candidatas = encontrar_consultas_candidatas(financiador_id, evento)
        cabecalho, pagamentos = _traduzir_evento_webhook(evento)

        if not candidatas:
            logger.warning(
                "[Processor] Nenhuma consulta casou com o evento (webhook_inbox_id=%s, financiador=%s, "
                "documentoUsuarioFinalRecebedor=%s, dataLiquidacao=%s) — indo para agenda_ur_orfa",
                webhook_inbox_id, financiador_id,
                evento.get("documentoUsuarioFinalRecebedor"), evento.get("dataLiquidacao"),
            )
            db.table("agenda_ur_orfa").insert({"payload": payload}).execute()
        else:
            upsert_agenda_ur(financiador_id, cabecalho, pagamentos)
            agora = datetime.now(timezone.utc)
            for consulta in candidatas:
                try:
                    db.table("consulta_agenda_ur").insert({
                        "consulta_id": consulta["id"],
                        "entidade_registradora": cabecalho["entidade_registradora"],
                        "cnpj_credenciadora": cabecalho["cnpj_credenciadora"],
                        "documento_ufr": cabecalho["documento_ufr"],
                        "documento_titular": cabecalho["documento_titular"],
                        "codigo_arranjo": cabecalho["codigo_arranjo"],
                        "data_liquidacao": cabecalho["data_liquidacao"],
                        "origem": "WEBHOOK",
                    }).execute()
                except DBAPIError as erro:
                    if not _violacao_unique(erro):
                        raise
                    continue  # já vinculado por uma tentativa anterior (redelivery do Pub/Sub) — não reincrementa
                db.table("consulta_agenda").update({
                    "qtd_urs_webhook": consulta["qtd_urs_webhook"] + 1,
                    "ultima_ur_em": agora,
                }).eq("id", consulta["id"]).eq("status", "PARCIAL").execute()
            logger.info(
                "[Processor] Evento correlacionado (webhook_inbox_id=%s, financiador=%s, consultas_casadas=%d)",
                webhook_inbox_id, financiador_id, len(candidatas),
            )
    except Exception as erro:
        logger.exception(
            "[Processor] Falha ao processar evento agenda (webhook_inbox_id=%s, financiador=%s)",
            webhook_inbox_id, financiador_id,
        )
        db.table("webhook_inbox").update({
            "processado_em": datetime.now(timezone.utc),
            "erro": str(erro),
        }).eq("id", webhook_inbox_id).execute()
        return JsonResponse({}, status=204)

    db.table("webhook_inbox").update({"processado_em": datetime.now(timezone.utc)}).eq("id", webhook_inbox_id).execute()
    return JsonResponse({}, status=204)


_TENANTS_JOBS_PERIODICOS = ["12345678000199"]  # lista fixa — design doc §14 ponto 3, não resolvido ainda
_QUIET_PERIOD_SEGUNDOS = 90
_HARD_TIMEOUT_SEGUNDOS = 15 * 60


@require_POST
def varrer_completude(request):
    """Job de completude (SPEC03 §5.5): sem sinal de fim do webhook, uma
    consulta ONLINE em PARCIAL vira COMPLETA após um quiet period sem
    novas URs, ou COMPLETA_COM_TIMEOUT após um hard timeout desde o início.
    Disparado por Cloud Scheduler, verificado pelo mesmo OIDC do consumidor
    Pub/Sub (design doc §8/Global Constraints deste plano)."""
    if not verificar_push_oidc(request):
        return JsonResponse({"erro": "OIDC inválido"}, status=401)

    agora = datetime.now(timezone.utc)
    resultado = {"completas": 0, "timeout": 0}

    for financiador_id in _TENANTS_JOBS_PERIODICOS:
        db = get_db(financiador_id)
        parciais = db.table("consulta_agenda").select("*").eq("status", "PARCIAL").eq("modo", "ONLINE").execute().data

        for consulta in parciais:
            referencia = consulta["ultima_ur_em"] or consulta["iniciada_em"]
            idade_desde_ultima_ur = (agora - referencia).total_seconds()
            idade_total = (agora - consulta["iniciada_em"]).total_seconds()

            if idade_total > _HARD_TIMEOUT_SEGUNDOS:
                db.table("consulta_agenda").update({
                    "status": "COMPLETA_COM_TIMEOUT", "encerrada_em": agora,
                }).eq("id", consulta["id"]).execute()
                logger.warning("[Completude] consulta %s COMPLETA_COM_TIMEOUT (financiador=%s)", consulta["id"], financiador_id)
                resultado["timeout"] += 1
            elif idade_desde_ultima_ur > _QUIET_PERIOD_SEGUNDOS:
                db.table("consulta_agenda").update({
                    "status": "COMPLETA", "encerrada_em": agora,
                }).eq("id", consulta["id"]).execute()
                resultado["completas"] += 1

    return JsonResponse(resultado, status=200)
