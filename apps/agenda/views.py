import base64
import hmac
import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from sqlalchemy.exc import DBAPIError
from ulid import ULID

from apps.agenda import parser_ap005
from apps.agenda.correlacao import encontrar_consultas_candidatas
from apps.agenda.importar_ap005 import importar_arquivo
from apps.agenda.repository import _CHAVE_UR, _buscar_um, _com_filtros, _como_datetime
from apps.agenda.repository import upsert_agenda_ur
from apps.agenda.validation import ValidacaoConsultaError, validar_modos_permitidos
from apps.agenda.validation import _FUSO_CONSULTA
from apps.agenda.webhook_dedupe import hash_evento
from services.cerc.client import (
    CercConsultaCriticaError,
    CercConsultaInvalidaError,
    CercConsultaRetentavelError,
    consultar_agenda,
)
from shared import pubsub_client
from shared.cloudsql_client import get_db
from shared.jwt_auth import jwt_required
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
        corpo = json.loads(request.body)
    except json.JSONDecodeError:
        logger.warning("[Webhook] Corpo não é JSON válido (financiador=%s)", financiador_id)
        return JsonResponse({"erro": "corpo não é JSON válido"}, status=400)

    # SPEC03 §5.2 documenta o envelope como objeto solto; o teste de
    # conectividade real do portal da CERC manda embrulhado num array de 1
    # elemento (achado em 2026-09-04, ver docs/runbooks/gcp-setup.md) —
    # aceita os dois formatos.
    envelope = corpo[0] if isinstance(corpo, list) and corpo else corpo

    tipo_evento = envelope.get("tipoEvento") if isinstance(envelope, dict) else None
    data_hora_evento = envelope.get("dataHoraEvento") if isinstance(envelope, dict) else None
    evento = envelope.get("evento") if isinstance(envelope, dict) else None

    # testeCerc (SPEC01 §4.4): ping de conectividade da CERC, sem UR real
    # — não carrega "evento". Confirmado em 2026-09-04: o teste do portal
    # manda só tipoEvento+dataHoraEvento. Para qualquer outro tipoEvento
    # (em particular "agenda"), os três campos continuam obrigatórios.
    if tipo_evento == "testeCerc":
        if not data_hora_evento:
            return JsonResponse({"erro": "envelope inválido: dataHoraEvento é obrigatório"}, status=400)
    elif not tipo_evento or not data_hora_evento or evento is None:
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


def _decimal(valor) -> Decimal:
    """SPEC-04 §1: dinheiro é Decimal na aplicação, nunca float. O corpo do
    webhook chega desserializado com `float` nativo do Python — este é o
    ponto de tradução onde vira Decimal antes de alcançar upsert_agenda_ur.
    `Decimal(str(v))`, não `Decimal(v)` direto: construir Decimal a partir do
    float reproduz o binário exato dele (`Decimal(1000.33)` vira
    `Decimal('1000.3299999999999954525264911353588104248046875')`); passar
    por `str()` usa a representação decimal mais curta que o Python já
    calcula para exibir o float — a mesma que corresponde ao literal JSON
    original para qualquer valor monetário na faixa de grandeza deste
    sistema (mesma técnica de services/cerc/client.py::_decimal)."""
    return Decimal(str(valor)) if valor is not None else None


def _traduzir_pagamento_webhook(pagamento: dict) -> dict:
    return {
        "tipo_informacao_pagamento": str(pagamento["tipoInformacaoPagamento"]),
        "indicador_efeitos_contrato": pagamento.get("indicadorEfeitosContrato"),
        "identificador_cerc_contrato": None,  # coluna 12.16 — só o Plano 08 (arquivo AP005) preenche
        "regras_divisao": pagamento.get("regrasDivisao"),
        "valor_onerado": _decimal(pagamento.get("valorOnerado")),
        "valor_constituido_efeito": _decimal(pagamento.get("valorConstituidoEfeito")),
        "valor_a_pagar": _decimal(pagamento.get("valorAPagar")),
        "beneficiario": pagamento.get("beneficiario"),
        "data_liquidacao_efetiva": pagamento.get("dataLiquidacaoEfetiva"),
        "valor_liquidacao_efetiva": _decimal(pagamento.get("valorLiquidacaoEfetiva")),
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
        "valor_constituido_total": _decimal(evento["valorConstituidoTotal"]),
        "valor_constituido_antecipacao_pre": _decimal(evento.get("valorConstituidoAntecipacaoPre", 0)),
        "valor_bloqueado": _decimal(evento.get("valorBloqueado", 0)),
        "valor_livre": _decimal(evento.get("valorLivre", 0)),
        "valor_total_ur": _decimal(evento["valorTotalUR"]),
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


_TENANTS_JOBS_PERIODICOS = ["38138785000136"]  # lista fixa — design doc §14 ponto 3, não resolvido ainda; CNPJ do tenant provisionado em homolog (docs/runbooks/gcp-setup.md)
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


class _StreamDeRequisicao(io.RawIOBase):
    """Adapta o HttpRequest do Django (que só expõe .read(n)/.readline()) à
    interface de leitura binária que io.TextIOWrapper exige (readable() +
    readinto()) dentro de importar_arquivo — sem materializar o corpo da
    requisição inteiro em memória, mantendo a leitura em stream (design doc
    §9/§14 item 4, mesma restrição descrita na docstring de importar_ap005)."""

    def __init__(self, request):
        self._request = request

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        dados = self._request.read(len(buffer))
        buffer[: len(dados)] = dados
        return len(dados)


@require_POST
def importar_ap005(request, financiador_id: str):
    """Endpoint interno de ingestão de arquivo AP005/AP005A/AP005B (design
    doc §9/§14 item 4). A conexão real (SFTP/Bucket/Connect:Direct) é uma
    dependência de infra ainda a definir — decisão do Plano 08 (Global
    Constraints): por ora, o arquivo chega aqui já em mãos (nome no header
    X-Nome-Arquivo, conteúdo no corpo bruto da requisição, lido via stream
    — nunca request.body, que materializaria tudo em memória), protegido
    pelo mesmo OIDC dos outros jobs internos. Um plano futuro liga isso a
    Cloud Scheduler + o canal real, quando a credencial existir."""
    if not verificar_push_oidc(request):
        return JsonResponse({"erro": "OIDC inválido"}, status=401)

    nome_arquivo = request.META.get("HTTP_X_NOME_ARQUIVO")
    if not nome_arquivo:
        return JsonResponse({"erro": "header X-Nome-Arquivo é obrigatório"}, status=400)

    try:
        resultado = importar_arquivo(financiador_id, nome_arquivo, _StreamDeRequisicao(request))
    except parser_ap005.NomeArquivoInvalidoError as exc:
        return JsonResponse({"erro": str(exc)}, status=400)
    except Exception:
        logger.exception(
            "[ImportarAP005] Falha ao importar arquivo (financiador=%s, arquivo=%s)", financiador_id, nome_arquivo,
        )
        return JsonResponse({"erro": "falha ao importar arquivo"}, status=500)

    status = 202 if resultado.get("ja_processado") else 200
    return JsonResponse(resultado, status=status)


_CODIGOS_VALIDACAO_403 = {
    "SEM_BASE_AUTORIZATIVA", "POLITICA_NAO_CONFIGURADA", "MODO_NAO_PERMITIDO",
    "RATE_LIMIT_EXCEDIDO", "CARTEIRA_OBRIGATORIA",
}


def _status_para_validacao(codigo: str) -> int:
    return 403 if codigo in _CODIGOS_VALIDACAO_403 else 422


def _parse_data_iso(valor, nome: str) -> date:
    if not valor:
        raise ValueError(f"{nome} é obrigatório")
    try:
        return date.fromisoformat(valor)
    except ValueError:
        raise ValueError(f"{nome} deve estar no formato AAAA-MM-DD: {valor!r}")


def _traduzir_requisicao_consulta(payload: dict, request) -> dict:
    base_autorizativa = payload.get("baseAutorizativa") or {}
    return {
        "modo": payload.get("modo"),
        "documento_ufr": payload.get("usuarioFinalRecebedor"),
        "documento_titular": payload.get("titular"),
        "credenciadoras": payload.get("credenciadoras") or [],
        "arranjos": payload.get("arranjos") or [],
        "data_inicio": _parse_data_iso(payload.get("dataInicio"), "dataInicio"),
        "data_fim": _parse_data_iso(payload.get("dataFim"), "dataFim"),
        "tipo_avaliacao": payload.get("tipoAvaliacao"),
        "participante": payload.get("participante"),
        "carteira": payload.get("carteira"),
        "base_autorizativa": {"tipo": base_autorizativa.get("tipo"), "id": base_autorizativa.get("id")},
        "motivo": payload.get("motivo"),
        "ator": request.jwt_claims.get("sub") or "desconhecido",
        "origem_ip": request.META.get("REMOTE_ADDR"),
    }


_CAMPOS_OBRIGATORIOS_CONSULTA = (
    "modo", "usuarioFinalRecebedor", "credenciadoras", "arranjos",
    "dataInicio", "dataFim", "baseAutorizativa", "motivo",
)


@jwt_required
@require_POST
def criar_consulta_agenda(request):
    """POST /api/v1/agendas/consultas (design doc §10, SPEC03 §7.1) —
    wrapper fino em cima de services.cerc.client.consultar_agenda (Plano
    06), que já roda A01-A10 e faz a chamada à CERC. Esta view só traduz
    o payload camelCase pro dict snake_case que consultar_agenda espera,
    mapeia exceções pra status HTTP (Global Constraints deste plano), e
    preenche ator/origem_ip a partir do JWT — nunca do corpo."""
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"erro": "CORPO_INVALIDO", "mensagem": "corpo não é JSON válido"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"erro": "CORPO_INVALIDO", "mensagem": "corpo deve ser um objeto JSON"}, status=400)

    faltando = [campo for campo in _CAMPOS_OBRIGATORIOS_CONSULTA if not payload.get(campo)]
    if faltando:
        return JsonResponse(
            {"erro": "CAMPO_OBRIGATORIO_AUSENTE", "mensagem": f"campos obrigatórios ausentes: {', '.join(faltando)}"},
            status=400,
        )

    if payload["modo"] not in ("ONLINE", "BATCH"):
        return JsonResponse({"erro": "MODO_INVALIDO", "mensagem": "modo deve ser ONLINE ou BATCH"}, status=400)

    try:
        consulta = _traduzir_requisicao_consulta(payload, request)
    except (ValueError, TypeError, AttributeError) as exc:
        # TypeError/AttributeError cobrem formatos inesperados nos campos
        # (ex.: dataInicio não-string, baseAutorizativa não-dict) — são erro
        # do cliente (400), não falha inesperada (500).
        return JsonResponse({"erro": "DATA_INVALIDA", "mensagem": str(exc)}, status=400)

    try:
        resultado = consultar_agenda(request.financiador_id, consulta)
    except ValidacaoConsultaError as exc:
        return JsonResponse({"erro": exc.codigo, "mensagem": exc.mensagem}, status=_status_para_validacao(exc.codigo))
    except CercConsultaCriticaError as exc:
        logger.error("[Consultas] Erro crítico da CERC (financiador=%s): %s", request.financiador_id, exc)
        return JsonResponse({"erro": exc.codigo, "mensagem": exc.mensagem}, status=502)
    except CercConsultaRetentavelError as exc:
        return JsonResponse({"erro": exc.codigo, "mensagem": exc.mensagem}, status=503)
    except CercConsultaInvalidaError as exc:
        return JsonResponse({"erro": exc.codigo, "mensagem": exc.mensagem}, status=422)
    except Exception:
        logger.exception("[Consultas] Falha inesperada ao criar consulta (financiador=%s)", request.financiador_id)
        return JsonResponse({"erro": "ERRO_INTERNO", "mensagem": "falha inesperada ao processar a consulta"}, status=500)

    status_http = 200 if consulta["modo"] == "BATCH" else 202
    return JsonResponse(
        {"consultaId": resultado["consultaId"], "status": resultado["status"], "agendas": resultado["agendas"]},
        status=status_http,
    )


def _contagem_e_frescor_por_origem(db, consulta_id: str) -> tuple:
    """Única fonte de verdade pra contagem por origem desde que Task 2
    deste plano passou a gravar SINCRONO em consulta_agenda_ur (fecha
    design doc §15 risco 14) — WEBHOOK já era gravado pelo Plano 07.
    ARQUIVO fica sempre 0: a ingestão de arquivo (Plano 08) nunca associa
    uma UR a uma consulta_agenda, não existe consulta nesse fluxo."""
    vinculos = db.table("consulta_agenda_ur").select("*").eq("consulta_id", consulta_id).execute().data
    contagem = {"SINCRONO": 0, "WEBHOOK": 0, "ARQUIVO": 0}
    timestamps = []
    for vinculo in vinculos:
        contagem[vinculo["origem"]] = contagem.get(vinculo["origem"], 0) + 1
        chave = {campo: vinculo[campo] for campo in _CHAVE_UR}
        ur = _buscar_um(db, "agenda_ur", chave, _CHAVE_UR)
        if ur:
            timestamps.append(_como_datetime(ur["data_hora_ultima_atualizacao"]))

    frescor = None
    if timestamps:
        frescor = {"maisAntigo": min(timestamps).isoformat(), "maisRecente": max(timestamps).isoformat()}
    return contagem, frescor


def _serializar_consulta(consulta: dict, contagem: dict, frescor) -> dict:
    return {
        "consultaId": consulta["id"],
        "modo": consulta["modo"],
        "status": consulta["status"],
        "filtroUfr": consulta["filtro_ufr"],
        "filtroTitular": consulta["filtro_titular"],
        "filtroCredenciadoras": consulta["filtro_credenciadoras"],
        "filtroArranjos": consulta["filtro_arranjos"],
        "filtroDataInicio": consulta["filtro_data_inicio"].isoformat(),
        "filtroDataFim": consulta["filtro_data_fim"].isoformat(),
        "contagemPorOrigem": contagem,
        "frescor": frescor,
        "iniciadaEm": consulta["iniciada_em"].isoformat(),
        "encerradaEm": consulta["encerrada_em"].isoformat() if consulta["encerrada_em"] else None,
    }


@jwt_required
@require_GET
def obter_consulta_agenda(request, consulta_id: str):
    """GET /api/v1/agendas/consultas/{id} (design doc §10, SPEC03 §7.2)."""
    try:
        db = get_db(request.financiador_id)
        linhas = db.table("consulta_agenda").select("*").eq("id", consulta_id).execute().data
        if not linhas:
            return JsonResponse(
                {"erro": "CONSULTA_NAO_ENCONTRADA", "mensagem": f"consulta {consulta_id!r} não encontrada"}, status=404,
            )

        contagem, frescor = _contagem_e_frescor_por_origem(db, consulta_id)
        return JsonResponse(_serializar_consulta(linhas[0], contagem, frescor), status=200)
    except Exception:
        logger.exception(
            "[Consultas] Falha inesperada ao obter consulta (financiador=%s, consulta_id=%s)",
            request.financiador_id, consulta_id,
        )
        return JsonResponse({"erro": "ERRO_INTERNO", "mensagem": "falha inesperada ao processar a consulta"}, status=500)


def _serializar_politica(politica: dict) -> dict:
    return {
        "id": politica["id"],
        "motivo": politica["motivo"],
        "modosPermitidos": politica["modos_permitidos"],
        "ativo": politica["ativo"],
        "criadoEm": politica["criado_em"].isoformat(),
        "atualizadoEm": politica["atualizado_em"].isoformat(),
    }


def _listar_politicas(request):
    db = get_db(request.financiador_id)
    politicas = db.table("politica_consulta").select("*").execute().data
    return JsonResponse({"politicas": [_serializar_politica(p) for p in politicas]}, status=200)


def _upsert_politica(request):
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"erro": "CORPO_INVALIDO", "mensagem": "corpo não é JSON válido"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"erro": "CORPO_INVALIDO", "mensagem": "corpo deve ser um objeto JSON"}, status=400)

    motivo = payload.get("motivo")
    if not motivo:
        return JsonResponse({"erro": "CAMPO_OBRIGATORIO_AUSENTE", "mensagem": "motivo é obrigatório"}, status=400)
    if not isinstance(motivo, str):
        return JsonResponse(
            {"erro": "CAMPO_OBRIGATORIO_AUSENTE", "mensagem": "motivo deve ser uma string"}, status=400,
        )

    try:
        validar_modos_permitidos(payload.get("modosPermitidos"))
    except ValidacaoConsultaError as exc:
        return JsonResponse({"erro": exc.codigo, "mensagem": exc.mensagem}, status=400)

    ativo = payload.get("ativo", True)
    db = get_db(request.financiador_id)
    dados = {
        "modos_permitidos": payload["modosPermitidos"], "ativo": ativo,
        "atualizado_em": datetime.now(timezone.utc),
    }
    existente = db.table("politica_consulta").select("id").eq("motivo", motivo).execute().data
    if existente:
        politica = db.table("politica_consulta").update(dados).eq("motivo", motivo).execute().data[0]
    else:
        politica = db.table("politica_consulta").insert({"id": str(ULID()), "motivo": motivo, **dados}).execute().data[0]

    return JsonResponse(_serializar_politica(politica), status=200)


def _desativar_politica(request):
    motivo = request.GET.get("motivo")
    if not motivo:
        return JsonResponse(
            {"erro": "CAMPO_OBRIGATORIO_AUSENTE", "mensagem": "motivo é obrigatório (query string)"}, status=400,
        )

    db = get_db(request.financiador_id)
    existente = db.table("politica_consulta").select("*").eq("motivo", motivo).execute().data
    if not existente:
        return JsonResponse(
            {"erro": "POLITICA_NAO_ENCONTRADA", "mensagem": f"nenhuma política para o motivo {motivo!r}"}, status=404,
        )

    politica = db.table("politica_consulta").update({
        "ativo": False, "atualizado_em": datetime.now(timezone.utc),
    }).eq("motivo", motivo).execute().data[0]
    return JsonResponse(_serializar_politica(politica), status=200)


@jwt_required
@require_http_methods(["GET", "PUT", "DELETE"])
def politicas_consulta(request):
    """GET/PUT/DELETE /api/v1/config/politicas-consulta (design doc §7)
    — self-service: cada financiador só vê/edita a própria política,
    automático porque cada um tem seu próprio banco (nenhuma checagem de
    propriedade extra é necessária). DELETE nunca apaga a linha — só
    desativa (Global Constraints deste plano)."""
    try:
        if request.method == "GET":
            return _listar_politicas(request)
        if request.method == "PUT":
            return _upsert_politica(request)
        return _desativar_politica(request)
    except Exception:
        logger.exception(
            "[PoliticasConsulta] Falha inesperada (financiador=%s, method=%s)",
            request.financiador_id, request.method,
        )
        return JsonResponse({"erro": "ERRO_INTERNO", "mensagem": "falha inesperada ao processar a política"}, status=500)


_LIMITE_PADRAO_LISTAGEM = 100
_LIMITE_MAXIMO_LISTAGEM = 1000

_FILTROS_URS = {
    "ufr": "documento_ufr", "titular": "documento_titular",
    "credenciadora": "cnpj_credenciadora", "arranjo": "codigo_arranjo",
    "constituicao": "constituicao", "origem": "origem",
}


def _parse_limite(request, maximo: int = _LIMITE_MAXIMO_LISTAGEM) -> int:
    bruto = request.GET.get("limit")
    if not bruto:
        return _LIMITE_PADRAO_LISTAGEM
    try:
        valor = int(bruto)
    except ValueError:
        raise ValueError(f"limit deve ser um inteiro: {bruto!r}")
    if valor <= 0 or valor > maximo:
        raise ValueError(f"limit deve estar entre 1 e {maximo}: {valor}")
    return valor


def _pagina_com_cursor(query, campo_cursor: str, cursor, limite: int) -> tuple:
    """Keyset pagination genérica: busca limite+1 linhas ordenadas por
    campo_cursor, corta a linha extra e usa o cursor dela como
    proximoCursor — evita um SELECT COUNT(*) só pra saber se há mais
    página. Reaproveitada por listar_urs (Task 3) e relatorio_compliance
    (Task 5)."""
    if cursor is not None:
        query = query.gt(campo_cursor, cursor)
    linhas = query.order(campo_cursor).limit(limite + 1).execute().data
    tem_mais = len(linhas) > limite
    pagina = linhas[:limite]
    proximo_cursor = pagina[-1][campo_cursor] if tem_mais and pagina else None
    return pagina, proximo_cursor


def _serializar_ur(ur: dict) -> dict:
    return {
        "entidadeRegistradora": ur["entidade_registradora"],
        "cnpjCredenciadora": ur["cnpj_credenciadora"],
        "documentoUfr": ur["documento_ufr"],
        "documentoTitular": ur["documento_titular"],
        "codigoArranjo": ur["codigo_arranjo"],
        "dataLiquidacao": ur["data_liquidacao"].isoformat(),
        "constituicao": ur["constituicao"],
        "valorConstituidoTotal": ur["valor_constituido_total"],
        "valorConstituidoAntecipacaoPre": ur["valor_constituido_antecipacao_pre"],
        "valorBloqueado": ur["valor_bloqueado"],
        "valorLivre": ur["valor_livre"],
        "valorTotalUR": ur["valor_total_ur"],
        "carteira": ur["carteira"],
        "dataHoraUltimaAtualizacao": _como_datetime(ur["data_hora_ultima_atualizacao"]).isoformat(),
        "origem": ur["origem"],
        "origemArquivo": ur["origem_arquivo"],
    }


@jwt_required
@require_GET
def listar_urs(request):
    """GET /api/v1/agendas/urs (design doc §10, SPEC03 §7.3) — repositório
    consolidado, não chama a CERC. Paginação por cursor sobre
    agenda_ur.sequencia (Plano 10, Global Constraints)."""
    try:
        cursor_bruto = request.GET.get("cursor")
        cursor = int(cursor_bruto) if cursor_bruto else None
        limite = _parse_limite(request)
        data_liq_inicio_bruto = request.GET.get("dataLiquidacaoInicio")
        data_liq_fim_bruto = request.GET.get("dataLiquidacaoFim")
        atualizado_desde_bruto = request.GET.get("atualizadoDesde")
        data_liq_inicio = _parse_data_iso(data_liq_inicio_bruto, "dataLiquidacaoInicio") if data_liq_inicio_bruto else None
        data_liq_fim = _parse_data_iso(data_liq_fim_bruto, "dataLiquidacaoFim") if data_liq_fim_bruto else None
        atualizado_desde = datetime.fromisoformat(atualizado_desde_bruto) if atualizado_desde_bruto else None
    except ValueError as exc:
        return JsonResponse({"erro": "PARAMETRO_INVALIDO", "mensagem": str(exc)}, status=400)

    try:
        db = get_db(request.financiador_id)
        query = db.table("agenda_ur").select("*")
        for parametro, coluna in _FILTROS_URS.items():
            valor = request.GET.get(parametro)
            if valor:
                query = query.eq(coluna, valor)

        if data_liq_inicio is not None:
            query = query.gte("data_liquidacao", data_liq_inicio)
        if data_liq_fim is not None:
            query = query.lte("data_liquidacao", data_liq_fim)
        if atualizado_desde is not None:
            query = query.gte("atualizado_em", atualizado_desde)

        pagina, proximo_cursor = _pagina_com_cursor(query, "sequencia", cursor, limite)
        return JsonResponse(
            {"urs": [_serializar_ur(ur) for ur in pagina], "proximoCursor": proximo_cursor}, status=200,
        )
    except Exception:
        logger.exception("[AgendasUrs] Falha inesperada ao listar (financiador=%s)", request.financiador_id)
        return JsonResponse({"erro": "ERRO_INTERNO", "mensagem": "falha inesperada ao listar URs"}, status=500)


_CAMPOS_OBRIGATORIOS_POSICAO = ("ufr", "dataLiquidacaoInicio", "dataLiquidacaoFim")


def _aplicar_filtros_posicao(query, request, data_liq_inicio: date, data_liq_fim: date):
    query = query.eq("documento_ufr", request.GET["ufr"])
    query = query.gte("data_liquidacao", data_liq_inicio)
    query = query.lte("data_liquidacao", data_liq_fim)
    credenciadora = request.GET.get("credenciadora")
    arranjo = request.GET.get("arranjo")
    if credenciadora:
        query = query.eq("cnpj_credenciadora", credenciadora)
    if arranjo:
        query = query.eq("codigo_arranjo", arranjo)
    return query


@jwt_required
@require_GET
def posicao_urs(request):
    """GET /api/v1/agendas/urs/posicao (design doc §10, SPEC03 §7.4) —
    visão agregada de crédito por UFR/janela. valorFumaca (constituicao=2)
    é sempre segregado: nunca soma em valorTotalConstituido/porCredenciadora
    /porArranjo. Sem JOIN: valorOnerado roda como query separada sobre
    agenda_ur_pagamento com os mesmos filtros de coluna compartilhados —
    mas sem filtro de constituicao, ver limitação conhecida no comentário
    junto dessa query, abaixo."""
    faltando = [c for c in _CAMPOS_OBRIGATORIOS_POSICAO if not request.GET.get(c)]
    if faltando:
        return JsonResponse(
            {"erro": "CAMPO_OBRIGATORIO_AUSENTE", "mensagem": f"parâmetros obrigatórios ausentes: {', '.join(faltando)}"},
            status=400,
        )

    try:
        data_liq_inicio = _parse_data_iso(request.GET.get("dataLiquidacaoInicio"), "dataLiquidacaoInicio")
        data_liq_fim = _parse_data_iso(request.GET.get("dataLiquidacaoFim"), "dataLiquidacaoFim")
    except ValueError as exc:
        return JsonResponse({"erro": "PARAMETRO_INVALIDO", "mensagem": str(exc)}, status=400)

    try:
        db = get_db(request.financiador_id)

        por_constituicao = _aplicar_filtros_posicao(
            db.table("agenda_ur").select(
                "constituicao, COALESCE(SUM(valor_constituido_total),0) AS total, "
                "COALESCE(SUM(valor_bloqueado),0) AS bloqueado, COALESCE(SUM(valor_livre),0) AS livre"
            ),
            request, data_liq_inicio, data_liq_fim,
        ).group_by("constituicao").execute().data
        totais = {linha["constituicao"]: linha for linha in por_constituicao}
        constituido = totais.get("1", {"total": Decimal("0"), "bloqueado": Decimal("0"), "livre": Decimal("0")})
        fumaca = totais.get("2", {"total": Decimal("0")})

        por_credenciadora = _aplicar_filtros_posicao(
            db.table("agenda_ur").select(
                "cnpj_credenciadora, COALESCE(SUM(valor_constituido_total),0) AS total"
            ).eq("constituicao", "1"),
            request, data_liq_inicio, data_liq_fim,
        ).group_by("cnpj_credenciadora").execute().data

        por_arranjo = _aplicar_filtros_posicao(
            db.table("agenda_ur").select(
                "codigo_arranjo, COALESCE(SUM(valor_constituido_total),0) AS total"
            ).eq("constituicao", "1"),
            request, data_liq_inicio, data_liq_fim,
        ).group_by("codigo_arranjo").execute().data

        # valorOnerado não filtra por constituicao='1' porque agenda_ur_pagamento
        # não tem essa coluna, e este plano não tem suporte a JOIN/IN pra
        # alcançar agenda_ur.constituicao a partir daqui (limitação conhecida,
        # não um esquecimento). Assunção: URs fumaça (constituicao='2', ainda
        # não constituídas) não têm pagamentos/efeitos de liquidação associados
        # — representam uma expectativa futura, não uma UR real ainda. Reavaliar
        # junto do risco 20 (design doc) quando um filtro IN for construído.
        onerado = _aplicar_filtros_posicao(
            db.table("agenda_ur_pagamento").select("COALESCE(SUM(valor_onerado),0) AS total"),
            request, data_liq_inicio, data_liq_fim,
        ).execute().data
        valor_onerado = onerado[0]["total"]

        frescor_linhas = _aplicar_filtros_posicao(
            db.table("agenda_ur").select(
                "MIN(data_hora_ultima_atualizacao) AS mais_antigo, MAX(data_hora_ultima_atualizacao) AS mais_recente"
            ),
            request, data_liq_inicio, data_liq_fim,
        ).execute().data
        frescor = None
        if frescor_linhas and frescor_linhas[0]["mais_antigo"] is not None:
            frescor = {
                "maisAntigo": _como_datetime(frescor_linhas[0]["mais_antigo"]).isoformat(),
                "maisRecente": _como_datetime(frescor_linhas[0]["mais_recente"]).isoformat(),
            }

        return JsonResponse({
            "valorTotalConstituido": constituido["total"],
            "valorLivre": constituido["livre"],
            "valorBloqueado": constituido["bloqueado"],
            "valorOnerado": valor_onerado,
            "valorFumaca": fumaca["total"],
            "porCredenciadora": [
                {"cnpjCredenciadora": l["cnpj_credenciadora"], "valorTotalConstituido": l["total"]}
                for l in por_credenciadora
            ],
            "porArranjo": [
                {"codigoArranjo": l["codigo_arranjo"], "valorTotalConstituido": l["total"]}
                for l in por_arranjo
            ],
            "frescor": frescor,
        }, status=200)
    except Exception:
        logger.exception("[AgendasUrsPosicao] Falha inesperada (financiador=%s)", request.financiador_id)
        return JsonResponse({"erro": "ERRO_INTERNO", "mensagem": "falha inesperada ao calcular posição"}, status=500)


def _filtros_totais_urs(query, ufr: str, credenciadora, arranjo):
    query = query.eq("documento_ufr", ufr)
    if credenciadora:
        query = query.eq("cnpj_credenciadora", credenciadora)
    if arranjo:
        query = query.eq("codigo_arranjo", arranjo)
    return query


@jwt_required
@require_GET
def totais_urs(request):
    """GET /api/v1/agendas/urs/totais — 4 totalizadores fixos pro topo da
    tela de agenda (design doc §10, sem número de plano — pedido direto do
    front). Diferente de posicao_urs (que exige uma janela de
    dataLiquidacao escolhida pelo chamador): aqui não há parâmetro de
    janela — cada campo tem sua própria semântica de tempo, fixa:

    - bloqueado/disponivel: soma de TODAS as URs constituídas do UFR
      (passado + futuro, sem filtro de data) — descrevem o estado ATUAL
      da UR (bloqueada pra oneração ou livre pra uso), não uma previsão
      de quando liquida. Custo: SUM(...) WHERE documento_ufr=X usa o
      índice (documento_ufr, data_liquidacao) INCLUDE (...) já existente
      como index-only scan — proporcional às URs desse UFR, não ao
      tamanho da tabela inteira.
    - liquidadoHoje: soma de valor_liquidacao_efetiva (agenda_ur_pagamento)
      confirmado HOJE (calendário America/Sao_Paulo, mesma convenção de
      apps.agenda.validation._FUSO_CONSULTA) — confirmação real de
      pagamento, não a data agendada (agenda_ur.data_liquidacao).
    - totalALiquidar: total constituído MENOS tudo que já foi confirmado
      como liquidado (valor_liquidacao_efetiva) em qualquer data — "o que
      falta pagar de verdade", não uma soma de valor_total_ur (que
      duplicaria UR com múltiplos titulares, já que valor_total_ur se
      repete por titular — mesma ressalva de posicao_urs/§4.5).

    valorFumaca (constituicao='2') nunca entra em nenhum dos quatro,
    mesma convenção de posicao_urs.
    """
    ufr = request.GET.get("ufr")
    if not ufr:
        return JsonResponse(
            {"erro": "CAMPO_OBRIGATORIO_AUSENTE", "mensagem": "parâmetro obrigatório ausente: ufr"}, status=400,
        )
    credenciadora = request.GET.get("credenciadora")
    arranjo = request.GET.get("arranjo")
    hoje = datetime.now(_FUSO_CONSULTA).date()

    try:
        db = get_db(request.financiador_id)

        posicao = _filtros_totais_urs(
            db.table("agenda_ur").select(
                "COALESCE(SUM(valor_bloqueado),0) AS bloqueado, "
                "COALESCE(SUM(valor_livre),0) AS livre, "
                "COALESCE(SUM(valor_constituido_total),0) AS constituido"
            ).eq("constituicao", "1"),
            ufr, credenciadora, arranjo,
        ).execute().data[0]

        liquidado_hoje = _filtros_totais_urs(
            db.table("agenda_ur_pagamento").select(
                "COALESCE(SUM(valor_liquidacao_efetiva),0) AS total"
            ).eq("data_liquidacao_efetiva", hoje),
            ufr, credenciadora, arranjo,
        ).execute().data[0]["total"]

        liquidado_total = _filtros_totais_urs(
            db.table("agenda_ur_pagamento").select(
                "COALESCE(SUM(valor_liquidacao_efetiva),0) AS total"
            ),
            ufr, credenciadora, arranjo,
        ).execute().data[0]["total"]

        return JsonResponse({
            "bloqueado": posicao["bloqueado"],
            "disponivel": posicao["livre"],
            "liquidadoHoje": liquidado_hoje,
            "totalALiquidar": posicao["constituido"] - liquidado_total,
        }, status=200)
    except Exception:
        logger.exception("[AgendasUrsTotais] Falha inesperada (financiador=%s)", request.financiador_id)
        return JsonResponse({"erro": "ERRO_INTERNO", "mensagem": "falha inesperada ao calcular totais"}, status=500)


_CAMPOS_OBRIGATORIOS_PAGAMENTOS_UR = (
    "entidadeRegistradora", "credenciadora", "ufr", "titular", "arranjo", "dataLiquidacao",
)


def _serializar_pagamento(pagamento: dict) -> dict:
    return {
        "tipoInformacaoPagamento": pagamento["tipo_informacao_pagamento"],
        "indicadorEfeitosContrato": pagamento["indicador_efeitos_contrato"],
        "identificadorCercContrato": pagamento["identificador_cerc_contrato"],
        "regrasDivisao": pagamento["regras_divisao"],
        "valorOnerado": pagamento["valor_onerado"],
        "valorConstituidoEfeito": pagamento["valor_constituido_efeito"],
        "valorAPagar": pagamento["valor_a_pagar"],
        "beneficiario": pagamento["beneficiario"],
        "dataLiquidacaoEfetiva": pagamento["data_liquidacao_efetiva"].isoformat() if pagamento["data_liquidacao_efetiva"] else None,
        "valorLiquidacaoEfetiva": pagamento["valor_liquidacao_efetiva"],
        "motivoNaoPagamento": pagamento["motivo_nao_pagamento"],
        "domicilio": pagamento["domicilio"],
    }


@jwt_required
@require_GET
def pagamentos_ur(request):
    """GET /api/v1/agendas/urs/pagamentos — detalhe dos pagamentos/efeitos
    (agenda_ur_pagamento) de UMA UR, identificada pela sua chave natural via
    query params — os mesmos 6 campos que listar_urs já devolve em cada
    linha (entidadeRegistradora, credenciadora, ufr, titular, arranjo,
    dataLiquidacao). Não usa `agenda_ur.sequencia` como identificador: é um
    cursor de paginação interno, deliberadamente não exposto pela API
    (ver test_views_listar_urs.py::test_lista_filtrada_por_ufr). Repositório
    consolidado, não chama a CERC. Lista vazia é resultado válido (UR
    "baixada sem pagamentos a fazer", SPEC03 §6.2) — nunca erro."""
    faltando = [c for c in _CAMPOS_OBRIGATORIOS_PAGAMENTOS_UR if not request.GET.get(c)]
    if faltando:
        return JsonResponse(
            {"erro": "CAMPO_OBRIGATORIO_AUSENTE", "mensagem": f"parâmetros obrigatórios ausentes: {', '.join(faltando)}"},
            status=400,
        )

    try:
        data_liquidacao = _parse_data_iso(request.GET.get("dataLiquidacao"), "dataLiquidacao")
    except ValueError as exc:
        return JsonResponse({"erro": "PARAMETRO_INVALIDO", "mensagem": str(exc)}, status=400)

    chave = {
        "entidade_registradora": request.GET["entidadeRegistradora"],
        "cnpj_credenciadora": request.GET["credenciadora"],
        "documento_ufr": request.GET["ufr"],
        "documento_titular": request.GET["titular"],
        "codigo_arranjo": request.GET["arranjo"],
        "data_liquidacao": data_liquidacao,
    }

    try:
        db = get_db(request.financiador_id)
        pagamentos = _com_filtros(
            db.table("agenda_ur_pagamento").select("*"), chave, _CHAVE_UR,
        ).execute().data
        return JsonResponse({"pagamentos": [_serializar_pagamento(p) for p in pagamentos]}, status=200)
    except Exception:
        logger.exception("[AgendasUrsPagamentos] Falha inesperada (financiador=%s)", request.financiador_id)
        return JsonResponse({"erro": "ERRO_INTERNO", "mensagem": "falha inesperada ao listar pagamentos"}, status=500)


def _serializar_consulta_compliance(consulta: dict) -> dict:
    return {
        "consultaId": consulta["id"],
        "modo": consulta["modo"],
        "status": consulta["status"],
        "filtroUfr": consulta["filtro_ufr"],
        "filtroTitular": consulta["filtro_titular"],
        "filtroCredenciadoras": consulta["filtro_credenciadoras"],
        "filtroArranjos": consulta["filtro_arranjos"],
        "filtroDataInicio": consulta["filtro_data_inicio"].isoformat(),
        "filtroDataFim": consulta["filtro_data_fim"].isoformat(),
        "tipoAvaliacao": consulta["tipo_avaliacao"],
        "carteira": consulta["carteira"],
        "baseAutorizativaTipo": consulta["base_autorizativa_tipo"],
        "baseAutorizativaId": consulta["base_autorizativa_id"],
        "motivo": consulta["motivo"],
        "ator": consulta["ator"],
        "origemIp": consulta["origem_ip"],
        "qtdUrsSincrono": consulta["qtd_urs_sincrono"],
        "qtdUrsWebhook": consulta["qtd_urs_webhook"],
        "iniciadaEm": consulta["iniciada_em"].isoformat(),
        "ultimaUrEm": consulta["ultima_ur_em"].isoformat() if consulta["ultima_ur_em"] else None,
        "encerradaEm": consulta["encerrada_em"].isoformat() if consulta["encerrada_em"] else None,
    }


_CAMPOS_OBRIGATORIOS_RELATORIO = ("dataInicio", "dataFim")


@jwt_required
@require_GET
def relatorio_compliance(request):
    """GET /api/v1/compliance/relatorio (design doc §10/§11, SPEC03 §8
    item 5) — trilha de compliance completa, síncrona e paginada por
    cursor sobre consulta_agenda.id (ULID, já ordenável por tempo de
    criação — Global Constraints deste plano, sem coluna nova). Sem
    exportação CSV/job assíncrono — design doc §11 explicitamente adia
    isso pra quando houver necessidade comprovada."""
    faltando = [c for c in _CAMPOS_OBRIGATORIOS_RELATORIO if not request.GET.get(c)]
    if faltando:
        return JsonResponse(
            {"erro": "CAMPO_OBRIGATORIO_AUSENTE", "mensagem": f"parâmetros obrigatórios ausentes: {', '.join(faltando)}"},
            status=400,
        )

    try:
        data_inicio = _parse_data_iso(request.GET.get("dataInicio"), "dataInicio")
        data_fim = _parse_data_iso(request.GET.get("dataFim"), "dataFim")
        limite = _parse_limite(request)
    except ValueError as exc:
        return JsonResponse({"erro": "PARAMETRO_INVALIDO", "mensagem": str(exc)}, status=400)

    # Dia calendário em America/Sao_Paulo, não UTC (mesma convenção de
    # apps.agenda.validation._FUSO_CONSULTA/A08) — janela [início, fim+1dia)
    # tratada como fechada no fim via -1 microssegundo, já que QueryBuilder
    # não tem operador "menor que" (só gte/lte/gt).
    inicio_dt = datetime.combine(data_inicio, datetime.min.time(), tzinfo=_FUSO_CONSULTA)
    fim_dt = (
        datetime.combine(data_fim + timedelta(days=1), datetime.min.time(), tzinfo=_FUSO_CONSULTA)
        - timedelta(microseconds=1)
    )
    cursor = request.GET.get("cursor") or None

    try:
        db = get_db(request.financiador_id)
        query = db.table("consulta_agenda").select("*").gte("iniciada_em", inicio_dt).lte("iniciada_em", fim_dt)
        ufr = request.GET.get("ufr")
        ator = request.GET.get("ator")
        if ufr:
            query = query.eq("filtro_ufr", ufr)
        if ator:
            query = query.eq("ator", ator)

        pagina, proximo_cursor = _pagina_com_cursor(query, "id", cursor, limite)
        return JsonResponse(
            {"consultas": [_serializar_consulta_compliance(c) for c in pagina], "proximoCursor": proximo_cursor},
            status=200,
        )
    except Exception:
        logger.exception("[ComplianceRelatorio] Falha inesperada (financiador=%s)", request.financiador_id)
        return JsonResponse({"erro": "ERRO_INTERNO", "mensagem": "falha inesperada ao gerar relatório"}, status=500)
