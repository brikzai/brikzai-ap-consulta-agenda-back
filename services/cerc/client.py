"""Cliente de consulta de agenda da CERC (POST /v15/agenda/consultar,
design doc §8, SPEC03 §4).

consultar_agenda roda as validações A01-A10 (apps.agenda.validation) antes
de qualquer chamada, busca o token CERC (services.cerc.token_provider),
grava a trilha em cerc_requisicao antes de interpretar a resposta, traduz
cada UR (uma linha por titular — ver design doc/plan Global Constraints) e
persiste via apps.agenda.repository.upsert_agenda_ur (origem='SINCRONO').
Mantém consulta_agenda: BATCH fecha em COMPLETA na hora (não há webhook a
esperar); ONLINE abre em PARCIAL — o enriquecimento por webhook é
responsabilidade do Plano 07, que nunca chama esta função.

Mapeamento de erros CERC (catálogo 105xxx, SPEC03 §10):
- 105001 -> sucesso vazio (lista de agendas vazia) — nunca é exceção.
- 105003/105998/105999 -> CercConsultaRetentavelError (retentável; este
  cliente não faz retry automático de erro de negócio, só de token 401).
- 105802 -> CercConsultaCriticaError (não deveria ocorrer — A07 já barra
  antes; se chegar aqui é alerta crítico, design doc §8).
- qualquer outro código -> CercConsultaInvalidaError (equivalente a 422 local).
"""
import os
import uuid
from datetime import datetime, timezone

import httpx
from ulid import ULID

from apps.agenda.repository import upsert_agenda_ur
from apps.agenda.validation import validar_consulta
from services.cerc.token_provider import get_cerc_token, invalidate_token
from shared.cloudsql_client import get_db

_CAMINHO_CONSULTAR = "/v15/agenda/consultar"

_CODIGO_SUCESSO_VAZIO = "105001"
_CODIGO_CRITICO = "105802"
_CODIGOS_RETENTAVEIS = {"105003", "105998", "105999"}


class CercConsultaError(Exception):
    def __init__(self, codigo, mensagem):
        self.codigo = codigo
        self.mensagem = mensagem
        super().__init__(f"{codigo}: {mensagem}")


class CercConsultaRetentavelError(CercConsultaError):
    pass


class CercConsultaCriticaError(CercConsultaError):
    pass


class CercConsultaInvalidaError(CercConsultaError):
    pass


def _corpo_requisicao(consulta: dict) -> dict:
    body = {
        "listaCnpjCredenciadora": consulta["credenciadoras"],
        "documentoUsuarioFinalRecebedor": consulta["documento_ufr"],
        "listaCodigoArranjoPagamento": consulta["arranjos"],
        "dataInicio": consulta["data_inicio"].isoformat(),
        "dataFim": consulta["data_fim"].isoformat(),
    }
    if consulta.get("documento_titular"):
        body["documentoTitular"] = consulta["documento_titular"]
    if consulta.get("tipo_avaliacao"):
        body["tipoAvaliacao"] = consulta["tipo_avaliacao"]
    if consulta.get("participante"):
        body["participante"] = consulta["participante"]
    if consulta.get("carteira"):
        body["carteira"] = consulta["carteira"]
    return body


def _registrar_requisicao(financiador_id: str, correlacao_id: str, request_body: dict, *, http_status, response_body) -> None:
    get_db(financiador_id).table("cerc_requisicao").insert({
        "id": str(ULID()),
        "recurso": "agenda_consultar",
        "correlacao_id": correlacao_id,
        "http_status": http_status,
        "request_body": request_body,
        "response_body": response_body,
    }).execute()


def _tratar_erro_cerc(http_status: int, corpo_resposta) -> list:
    codigo = None
    mensagem = f"HTTP {http_status}"
    if isinstance(corpo_resposta, dict):
        erros = corpo_resposta.get("erros") or []
        if erros:
            codigo = str(erros[0].get("codigo"))
            mensagem = erros[0].get("mensagem", mensagem)

    if codigo == _CODIGO_SUCESSO_VAZIO:
        return []
    if codigo == _CODIGO_CRITICO:
        raise CercConsultaCriticaError(codigo, mensagem)
    if codigo in _CODIGOS_RETENTAVEIS:
        raise CercConsultaRetentavelError(codigo, mensagem)
    raise CercConsultaInvalidaError(codigo or str(http_status), mensagem)


def _chamar_cerc(financiador_id: str, consulta: dict, *, online: bool, tentativa_401: bool = False) -> list:
    token = get_cerc_token(financiador_id)
    body = _corpo_requisicao(consulta)
    correlacao_id = str(uuid.uuid4())

    try:
        response = httpx.post(
            f"{os.environ['CERC_API_BASE_URL']}{_CAMINHO_CONSULTAR}",
            params={"online": "true" if online else "false"},
            json=body,
            headers={"Authorization": f"Bearer {token}", "X-Correlation-Id": correlacao_id},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        _registrar_requisicao(financiador_id, correlacao_id, body, http_status=None, response_body=None)
        raise CercConsultaRetentavelError("105003", f"falha de comunicação com a CERC: {type(exc).__name__}") from None

    if response.status_code == 401 and not tentativa_401:
        invalidate_token(financiador_id)
        return _chamar_cerc(financiador_id, consulta, online=online, tentativa_401=True)

    corpo_resposta = response.json() if response.content else None
    _registrar_requisicao(financiador_id, correlacao_id, body, http_status=response.status_code, response_body=corpo_resposta)

    if response.status_code == 200:
        return corpo_resposta or []

    return _tratar_erro_cerc(response.status_code, corpo_resposta)


def _parse_data_hora(valor: str) -> datetime:
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))


def _traduzir_pagamento(pagamento: dict) -> dict:
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


def _traduzir_ur(agenda: dict, ur: dict) -> list:
    """Uma UR pode ser fracionada entre titulares (SPEC03 §4.3) — cada
    fração vira uma linha própria em agenda_ur, com os valores DO TITULAR
    (nunca os agregados do nível da UR, exceto valorTotalUR — §4.5)."""
    linhas = []
    for titular in ur["titulares"]:
        cabecalho = {
            "entidade_registradora": agenda["entidadeRegistradora"],
            "cnpj_credenciadora": agenda["instituicaoCredenciadora"],
            "codigo_arranjo": agenda["codigoArranjoPagamento"],
            "documento_ufr": agenda["documentoUsuarioFinalRecebedor"],
            "documento_titular": titular["documentoTitular"],
            "data_liquidacao": ur["dataLiquidacao"],
            "constituicao": ur["constituicao"],
            "valor_constituido_total": titular["valorConstituidoTotal"],
            "valor_constituido_antecipacao_pre": titular.get("valorConstituidoAntecipacaoPre", 0),
            "valor_bloqueado": titular.get("valorBloqueado", 0),
            "valor_livre": titular.get("valorLivre", 0),
            "valor_total_ur": ur["valorTotalUR"],
            "carteira": ur.get("carteira"),
            "data_hora_ultima_atualizacao": _parse_data_hora(titular["dataHoraUltimaAtualizacao"]),
            "origem": "SINCRONO",
            "origem_arquivo": None,
        }
        pagamentos = [_traduzir_pagamento(p) for p in titular.get("pagamentos", [])]
        linhas.append((cabecalho, pagamentos))
    return linhas


def _registrar_consulta_agenda(financiador_id: str, consulta: dict, *, online: bool, qtd_urs: int) -> str:
    agora = datetime.now(timezone.utc)
    dados = {
        "id": str(ULID()),
        "modo": consulta["modo"],
        "status": "PARCIAL" if online else "COMPLETA",
        "filtro_ufr": consulta["documento_ufr"],
        "filtro_titular": consulta.get("documento_titular"),
        "filtro_credenciadoras": consulta["credenciadoras"],
        "filtro_arranjos": consulta["arranjos"],
        "filtro_data_inicio": consulta["data_inicio"],
        "filtro_data_fim": consulta["data_fim"],
        "tipo_avaliacao": consulta.get("tipo_avaliacao"),
        "carteira": consulta.get("carteira"),
        "base_autorizativa_tipo": consulta["base_autorizativa"]["tipo"],
        "base_autorizativa_id": consulta["base_autorizativa"]["id"],
        "motivo": consulta["motivo"],
        "ator": consulta["ator"],
        "origem_ip": consulta.get("origem_ip"),
        "qtd_urs_sincrono": qtd_urs,
        "qtd_urs_webhook": 0,
    }
    if not online:
        dados["encerrada_em"] = agora
    if qtd_urs:
        dados["ultima_ur_em"] = agora
    registro = get_db(financiador_id).table("consulta_agenda").insert(dados).execute().data[0]
    return registro["id"]


def consultar_agenda(financiador_id: str, consulta: dict) -> dict:
    validar_consulta(financiador_id, consulta)

    online = consulta["modo"] == "ONLINE"
    agendas = _chamar_cerc(financiador_id, consulta, online=online)

    qtd_urs = 0
    for agenda in agendas:
        for ur in agenda.get("unidadesRecebiveis", []):
            for cabecalho, pagamentos in _traduzir_ur(agenda, ur):
                upsert_agenda_ur(financiador_id, cabecalho, pagamentos)
                qtd_urs += 1

    consulta_id = _registrar_consulta_agenda(financiador_id, consulta, online=online, qtd_urs=qtd_urs)

    return {
        "consultaId": consulta_id,
        "status": "PARCIAL" if online else "COMPLETA",
        "agendas": agendas,
    }
