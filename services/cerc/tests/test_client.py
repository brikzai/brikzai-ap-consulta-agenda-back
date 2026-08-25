from datetime import date, datetime, timezone

import httpx
import pytest
import respx

from apps.agenda import repository, validation
from services.cerc import client
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
CNPJ_VALIDO = "11222333000181"
CPF_VALIDO = "11144477735"
URL_CONSULTAR = "https://ap-homolog.cerc.inf.br/v15/agenda/consultar"

CHAVE_UR_TESTE = {
    "data_liquidacao": "2026-09-20",
    "entidade_registradora": "22246686000196",
    "cnpj_credenciadora": "36216798000150",
    "documento_ufr": CNPJ_VALIDO,
    "documento_titular": CNPJ_VALIDO,
    "codigo_arranjo": "VCC",
}


def _limpar():
    db = get_db(FINANCIADOR_TESTE)
    parcial = {"data_liquidacao": CHAVE_UR_TESTE["data_liquidacao"], "entidade_registradora": CHAVE_UR_TESTE["entidade_registradora"]}
    campos_parciais = ("data_liquidacao", "entidade_registradora")
    repository._com_filtros(db.table("agenda_ur_evento").delete(), parcial, campos_parciais).execute()
    repository._com_filtros(db.table("agenda_ur_pagamento").delete(), parcial, campos_parciais).execute()
    repository._com_filtros(db.table("agenda_ur").delete(), parcial, campos_parciais).execute()
    db.table("consulta_agenda").delete().eq("filtro_ufr", CNPJ_VALIDO).execute()
    db.table("cerc_requisicao").delete().eq("recurso", "agenda_consultar").execute()
    db.table("politica_consulta").delete().eq("motivo", "TESTE-CLIENT").execute()


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    monkeypatch.setenv("CERC_API_BASE_URL", "https://ap-homolog.cerc.inf.br")
    monkeypatch.setattr(client, "get_cerc_token", lambda financiador_id: "token-teste")
    monkeypatch.setattr(client, "invalidate_token", lambda financiador_id: None)

    _limpar()
    db = get_db(FINANCIADOR_TESTE)
    db.table("politica_consulta").insert({
        "id": "pol-teste-client", "motivo": "TESTE-CLIENT",
        "modos_permitidos": ["BATCH", "ONLINE"], "ativo": True,
    }).execute()
    yield
    _limpar()


def _consulta_base(**overrides):
    base = {
        "modo": "BATCH",
        "documento_ufr": CNPJ_VALIDO,
        "documento_titular": None,
        "credenciadoras": ["99T"],
        "arranjos": ["99T"],
        "data_inicio": date(2026, 9, 1),
        "data_fim": date(2026, 9, 30),
        "tipo_avaliacao": None,
        "participante": None,
        "carteira": None,
        "base_autorizativa": {"tipo": "OPTIN", "id": "opt_1"},
        "motivo": "TESTE-CLIENT",
        "ator": "teste@teste.com",
        "origem_ip": None,
    }
    base.update(overrides)
    return base


def _titular(documento: str, valor: float, **overrides):
    base = {
        "documentoTitular": documento,
        "valorConstituidoTotal": valor,
        "valorConstituidoAntecipacaoPre": 0.0,
        "valorBloqueado": 0.0,
        "valorLivre": valor,
        "dataHoraUltimaAtualizacao": "2026-09-19T10:00:00.000Z",
        "pagamentos": [],
    }
    base.update(overrides)
    return base


def _resposta_cerc(titulares=None, **overrides_ur):
    ur = {
        "dataLiquidacao": "2026-09-20",
        "constituicao": "1",
        "valorConstituidoTotal": 1000.0,
        "valorConstituidoAntecipacaoPre": 0.0,
        "valorBloqueado": 0.0,
        "valorLivre": 1000.0,
        "valorTotalUR": 1000.0,
        "dataHoraUltimaAtualizacao": "2026-09-19T10:00:00.000Z",
        "pagamentos": [],
        "titulares": titulares if titulares is not None else [_titular(CNPJ_VALIDO, 1000.0)],
    }
    ur.update(overrides_ur)
    return [{
        "entidadeRegistradora": "22246686000196",
        "instituicaoCredenciadora": "36216798000150",
        "codigoArranjoPagamento": "VCC",
        "documentoUsuarioFinalRecebedor": CNPJ_VALIDO,
        "indicadoresConsistencia": [],
        "unidadesRecebiveis": [ur],
    }]


@respx.mock
def test_consultar_agenda_batch_persiste_ur_e_fecha_completa():
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc()))

    resultado = client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base(modo="BATCH"))

    assert resultado["status"] == "COMPLETA"
    assert resultado["consultaId"]

    db = get_db(FINANCIADOR_TESTE)
    ur_gravada = repository._com_filtros(db.table("agenda_ur").select("*"), CHAVE_UR_TESTE, repository._CHAVE_UR).execute().data
    assert len(ur_gravada) == 1
    assert ur_gravada[0]["origem"] == "SINCRONO"

    consulta = db.table("consulta_agenda").select("*").eq("id", resultado["consultaId"]).execute().data[0]
    assert consulta["status"] == "COMPLETA"
    assert consulta["qtd_urs_sincrono"] == 1
    assert consulta["encerrada_em"] is not None


@respx.mock
def test_consultar_agenda_online_abre_parcial():
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc()))

    resultado = client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base(modo="ONLINE"))

    assert resultado["status"] == "PARCIAL"
    db = get_db(FINANCIADOR_TESTE)
    consulta = db.table("consulta_agenda").select("*").eq("id", resultado["consultaId"]).execute().data[0]
    assert consulta["status"] == "PARCIAL"
    assert consulta["encerrada_em"] is None


@respx.mock
def test_consultar_agenda_persiste_uma_linha_por_titular():
    titulares = [_titular(CNPJ_VALIDO, 500.0), _titular(CPF_VALIDO, 500.0)]
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc(titulares=titulares)))

    client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    db = get_db(FINANCIADOR_TESTE)
    linhas = (
        db.table("agenda_ur").select("documento_titular,valor_total_ur")
        .eq("data_liquidacao", "2026-09-20")
        .eq("entidade_registradora", "22246686000196")
        .execute().data
    )
    assert {r["documento_titular"] for r in linhas} == {CNPJ_VALIDO, CPF_VALIDO}
    assert all(r["valor_total_ur"] == 1000 for r in linhas)  # valorTotalUR é do nível da UR, igual pros dois titulares


@respx.mock
def test_consultar_agenda_codigo_105001_retorna_lista_vazia_sem_erro():
    respx.post(URL_CONSULTAR).mock(
        return_value=httpx.Response(422, json={"erros": [{"codigo": 105001, "mensagem": "nada encontrado"}]})
    )

    resultado = client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    assert resultado["agendas"] == []
    assert resultado["status"] == "COMPLETA"


@respx.mock
def test_consultar_agenda_codigo_105003_e_retentavel():
    respx.post(URL_CONSULTAR).mock(
        return_value=httpx.Response(422, json={"erros": [{"codigo": 105003, "mensagem": "falha na registradora"}]})
    )

    with pytest.raises(client.CercConsultaRetentavelError) as exc_info:
        client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())
    assert exc_info.value.codigo == "105003"


@respx.mock
def test_consultar_agenda_codigo_105802_e_critico():
    respx.post(URL_CONSULTAR).mock(
        return_value=httpx.Response(403, json={"erros": [{"codigo": 105802, "mensagem": "opt-in não encontrado"}]})
    )

    with pytest.raises(client.CercConsultaCriticaError):
        client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())


@respx.mock
def test_consultar_agenda_erro_de_validacao_gera_erro_invalido():
    respx.post(URL_CONSULTAR).mock(
        return_value=httpx.Response(422, json={"erros": [{"codigo": 105009, "mensagem": "arranjo inválido"}]})
    )

    with pytest.raises(client.CercConsultaInvalidaError) as exc_info:
        client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())
    assert exc_info.value.codigo == "105009"


@respx.mock
def test_consultar_agenda_bloqueia_localmente_antes_de_chamar_cerc():
    route = respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=[]))

    with pytest.raises(validation.ValidacaoConsultaError):
        client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base(motivo="MOTIVO-SEM-POLITICA-CLIENT"))

    assert route.call_count == 0


@respx.mock
def test_consultar_agenda_retenta_uma_vez_em_401(monkeypatch):
    chamadas_invalidacao = []
    monkeypatch.setattr(client, "invalidate_token", lambda financiador_id: chamadas_invalidacao.append(financiador_id))

    route = respx.post(URL_CONSULTAR).mock(
        side_effect=[
            httpx.Response(401, json={"erro": "token expirado"}),
            httpx.Response(200, json=_resposta_cerc()),
        ]
    )

    resultado = client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    assert route.call_count == 2
    assert chamadas_invalidacao == [FINANCIADOR_TESTE]
    assert resultado["status"] == "COMPLETA"


@respx.mock
def test_consultar_agenda_grava_cerc_requisicao_antes_de_interpretar_resposta():
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc()))

    client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    db = get_db(FINANCIADOR_TESTE)
    requisicoes = db.table("cerc_requisicao").select("*").eq("recurso", "agenda_consultar").execute().data
    assert len(requisicoes) == 1
    assert requisicoes[0]["http_status"] == 200
