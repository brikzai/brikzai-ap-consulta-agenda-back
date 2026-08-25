"""Cliente de consulta de agenda da CERC (POST /v15/agenda/consultar,
design doc §8, SPEC03 §4).

consultar_agenda roda as validações A01-A10 (apps.agenda.validation) antes
de qualquer chamada, busca o token CERC (services.cerc.token_provider),
grava a trilha em cerc_requisicao antes de interpretar a resposta, traduz
cada UR (uma linha por titular — ver design doc/plan Global Constraints) e
persiste via apps.agenda.repository.upsert_agenda_ur (origem='SINCRONO').
consulta_agenda é criada em PARCIAL ANTES de chamar a CERC (trilha de
compliance + correlação com webhooks do Plano 07 desde o primeiro instante)
e fechada depois: COMPLETA (BATCH com sucesso), PARCIAL (ONLINE com sucesso,
aguardando enriquecimento por webhook do Plano 07, que nunca chama esta
função) ou ERRO (falha na chamada à CERC).

Mapeamento de erros CERC (catálogo 105xxx, SPEC03 §10):
- 105001 -> sucesso vazio (lista de agendas vazia) — nunca é exceção.
- 105003/105998/105999 -> CercConsultaRetentavelError (retentável; este
  cliente não faz retry automático de erro de negócio, só de token 401).
- 105801/105802 -> CercConsultaCriticaError (não deveria ocorrer — A07 já
  barra antes; se chegar aqui é alerta crítico, design doc §8/SPEC03 §10-11).
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
_CODIGOS_CRITICOS = {"105801", "105802"}
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


def _registrar_requisicao(financiador_id: str, correlacao_id: str, request_body: dict, *, http_status, response_body, tentativa: int = 1) -> None:
    get_db(financiador_id).table("cerc_requisicao").insert({
        "id": str(ULID()),
        "recurso": "agenda_consultar",
        "correlacao_id": correlacao_id,
        "http_status": http_status,
        "request_body": request_body,
        "response_body": response_body,
        "tentativa": tentativa,
    }).execute()


def _tratar_erro_cerc(http_status: int, corpo_resposta) -> list:
    codigo = None
    mensagem = f"HTTP {http_status}"
    if isinstance(corpo_resposta, dict):
        erros = corpo_resposta.get("erros") or []
        if erros and isinstance(erros[0], dict):
            codigo_bruto = erros[0].get("codigo")
            codigo = str(codigo_bruto) if codigo_bruto is not None else None
            mensagem = erros[0].get("mensagem", mensagem)

    if codigo == _CODIGO_SUCESSO_VAZIO:
        return []
    if codigo in _CODIGOS_CRITICOS:
        raise CercConsultaCriticaError(codigo, mensagem)
    if codigo in _CODIGOS_RETENTAVEIS:
        raise CercConsultaRetentavelError(codigo, mensagem)
    if codigo is None:
        if http_status in (401, 403):
            raise CercConsultaCriticaError(str(http_status), mensagem)
        if http_status == 429 or http_status >= 500:
            raise CercConsultaRetentavelError(str(http_status), mensagem)
    raise CercConsultaInvalidaError(codigo or str(http_status), mensagem)


def _ler_corpo(response: httpx.Response):
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _chamar_cerc(financiador_id: str, consulta: dict, *, online: bool, tentativa_401: bool = False, correlacao_id: str = None, tentativa: int = 1) -> list:
    token = get_cerc_token(financiador_id)
    body = _corpo_requisicao(consulta)
    if correlacao_id is None:
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
        _registrar_requisicao(financiador_id, correlacao_id, body, http_status=None, response_body=None, tentativa=tentativa)
        raise CercConsultaRetentavelError("105003", f"falha de comunicação com a CERC: {type(exc).__name__}") from None

    corpo_resposta = _ler_corpo(response)
    _registrar_requisicao(financiador_id, correlacao_id, body, http_status=response.status_code, response_body=corpo_resposta, tentativa=tentativa)

    if response.status_code == 401 and not tentativa_401:
        invalidate_token(financiador_id)
        return _chamar_cerc(financiador_id, consulta, online=online, tentativa_401=True, correlacao_id=correlacao_id, tentativa=2)

    if response.status_code == 200:
        if corpo_resposta is None:
            return []
        if not isinstance(corpo_resposta, list):
            raise CercConsultaInvalidaError("FORMATO_INESPERADO", "resposta 200 da CERC não é uma lista de agendas")
        return corpo_resposta

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
        pagamentos_titular = titular.get("pagamentos") or ur.get("pagamentos", [])
        pagamentos = [_traduzir_pagamento(p) for p in pagamentos_titular]
        linhas.append((cabecalho, pagamentos))
    return linhas


def _registrar_ur_rejeitada(financiador_id: str, ur_bruta: dict, erro: Exception) -> None:
    get_db(financiador_id).table("agenda_ur_rejeitada").insert({
        "origem": "SINCRONO",
        "arquivo": None,
        "linha": None,
        "conteudo": repr(ur_bruta),
        "motivo": f"{type(erro).__name__}: {erro}",
    }).execute()


def _criar_consulta_agenda(financiador_id: str, consulta: dict) -> str:
    dados = {
        "id": str(ULID()),
        "modo": consulta["modo"],
        "status": "PARCIAL",
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
        "qtd_urs_sincrono": 0,
        "qtd_urs_webhook": 0,
    }
    registro = get_db(financiador_id).table("consulta_agenda").insert(dados).execute().data[0]
    return registro["id"]


def _fechar_consulta_agenda(financiador_id: str, consulta_id: str, *, status: str, qtd_urs: int) -> None:
    agora = datetime.now(timezone.utc)
    dados = {"status": status, "qtd_urs_sincrono": qtd_urs}
    if qtd_urs:
        dados["ultima_ur_em"] = agora
    if status != "PARCIAL":
        dados["encerrada_em"] = agora
    get_db(financiador_id).table("consulta_agenda").update(dados).eq("id", consulta_id).execute()


def consultar_agenda(financiador_id: str, consulta: dict) -> dict:
    validar_consulta(financiador_id, consulta)

    online = consulta["modo"] == "ONLINE"
    consulta_id = _criar_consulta_agenda(financiador_id, consulta)

    try:
        agendas = _chamar_cerc(financiador_id, consulta, online=online)
    except Exception:
        # Qualquer falha ao obter o token ou chamar a CERC (não só
        # CercConsultaError — inclui CercTokenError e erros de banco na
        # própria trilha de auditoria) precisa fechar a consulta como ERRO,
        # senão ela fica presa em PARCIAL para sempre (design doc §15 risco 15).
        _fechar_consulta_agenda(financiador_id, consulta_id, status="ERRO", qtd_urs=0)
        raise

    qtd_urs = 0
    for agenda in agendas:
        for ur in agenda.get("unidadesRecebiveis", []):
            try:
                linhas = _traduzir_ur(agenda, ur)
                for cabecalho, pagamentos in linhas:
                    upsert_agenda_ur(financiador_id, cabecalho, pagamentos)
                    qtd_urs += 1
            except Exception as exc:
                _registrar_ur_rejeitada(financiador_id, ur, exc)

    status_final = "PARCIAL" if online else "COMPLETA"
    _fechar_consulta_agenda(financiador_id, consulta_id, status=status_final, qtd_urs=qtd_urs)

    return {
        "consultaId": consulta_id,
        "status": status_final,
        "agendas": agendas,
    }
