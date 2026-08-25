import json
from datetime import date, datetime, timezone

import httpx
import pytest
import respx
from ulid import ULID

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
        "id": str(ULID()), "motivo": "TESTE-CLIENT",
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
    titulares = [
        _titular(CNPJ_VALIDO, 600.0, valorBloqueado=100.0, valorLivre=500.0),
        _titular(CPF_VALIDO, 400.0, valorBloqueado=50.0, valorLivre=350.0),
    ]
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc(titulares=titulares)))

    client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    db = get_db(FINANCIADOR_TESTE)
    linhas = (
        db.table("agenda_ur").select("documento_titular,valor_total_ur,valor_constituido_total,valor_bloqueado,valor_livre")
        .eq("data_liquidacao", "2026-09-20")
        .eq("entidade_registradora", "22246686000196")
        .execute().data
    )
    assert {r["documento_titular"] for r in linhas} == {CNPJ_VALIDO, CPF_VALIDO}
    assert all(r["valor_total_ur"] == 1000 for r in linhas)  # valorTotalUR é do nível da UR, igual pros dois titulares
    # Verify per-titular values are persisted correctly
    cnpj_row = [r for r in linhas if r["documento_titular"] == CNPJ_VALIDO][0]
    assert cnpj_row["valor_constituido_total"] == 600.0
    assert cnpj_row["valor_bloqueado"] == 100.0
    assert cnpj_row["valor_livre"] == 500.0
    cpf_row = [r for r in linhas if r["documento_titular"] == CPF_VALIDO][0]
    assert cpf_row["valor_constituido_total"] == 400.0
    assert cpf_row["valor_bloqueado"] == 50.0
    assert cpf_row["valor_livre"] == 350.0


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


@respx.mock
def test_consultar_agenda_traduz_pagamento():
    titulares = [
        _titular(
            CNPJ_VALIDO,
            1000.0,
            pagamentos=[
                {
                    "tipoInformacaoPagamento": "AGENDA",
                    "indicadorEfeitosContrato": "SIM",
                    "regrasDivisao": "PROPORCIONAL",
                    "valorOnerado": 500.0,
                    "valorConstituidoEfeito": 450.0,
                    "valorAPagar": 450.0,
                    "beneficiario": "Banco Teste",
                    "dataLiquidacaoEfetiva": "2026-09-15",
                    "valorLiquidacaoEfetiva": 450.0,
                    "motivoDeNaoPagamento": None,
                    "domicilioPagamento": {
                        "banco": "001",
                        "agencia": "0001",
                        "conta": "123456",
                    },
                }
            ],
        )
    ]
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc(titulares=titulares)))

    client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    db = get_db(FINANCIADOR_TESTE)
    pagamentos = (
        db.table("agenda_ur_pagamento").select("*")
        .eq("data_liquidacao", "2026-09-20")
        .eq("entidade_registradora", "22246686000196")
        .execute().data
    )
    assert len(pagamentos) == 1
    pag = pagamentos[0]
    assert pag["tipo_informacao_pagamento"] == "AGENDA"
    assert pag["indicador_efeitos_contrato"] == "SIM"
    assert pag["identificador_cerc_contrato"] is None
    assert pag["regras_divisao"] == "PROPORCIONAL"
    assert pag["valor_onerado"] == 500.0
    assert pag["valor_constituido_efeito"] == 450.0
    assert pag["valor_a_pagar"] == 450.0
    assert pag["beneficiario"] == "Banco Teste"
    assert pag["data_liquidacao_efetiva"] == date(2026, 9, 15)
    assert pag["valor_liquidacao_efetiva"] == 450.0
    assert pag["motivo_nao_pagamento"] is None
    assert pag["domicilio"] == {"banco": "001", "agencia": "0001", "conta": "123456"}


@respx.mock
def test_consultar_agenda_correlacao_id_e_tentativa_em_401_retry(monkeypatch):
    chamadas_invalidacao = []
    monkeypatch.setattr(client, "invalidate_token", lambda financiador_id: chamadas_invalidacao.append(financiador_id))

    route = respx.post(URL_CONSULTAR).mock(
        side_effect=[
            httpx.Response(401, json={"erro": "token expirado"}),
            httpx.Response(200, json=_resposta_cerc()),
        ]
    )

    client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    db = get_db(FINANCIADOR_TESTE)
    requisicoes = (
        db.table("cerc_requisicao").select("correlacao_id,tentativa")
        .eq("recurso", "agenda_consultar")
        .execute().data
    )
    assert len(requisicoes) == 2
    # Both requests should share the same correlacao_id
    assert requisicoes[0]["correlacao_id"] == requisicoes[1]["correlacao_id"]
    # First attempt should have tentativa=1, second should have tentativa=2
    assert requisicoes[0]["tentativa"] == 1
    assert requisicoes[1]["tentativa"] == 2


@respx.mock
def test_consultar_agenda_envia_online_true_no_modo_online():
    route = respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc()))
    client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base(modo="ONLINE"))
    assert route.calls.last.request.url.params["online"] == "true"


@respx.mock
def test_consultar_agenda_envia_online_false_no_modo_batch():
    route = respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc()))
    client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base(modo="BATCH"))
    assert route.calls.last.request.url.params["online"] == "false"


@respx.mock
def test_consultar_agenda_monta_corpo_da_requisicao_corretamente():
    route = respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc()))

    client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base(
        documento_titular=CPF_VALIDO, tipo_avaliacao="avaliacao_agenda_basica_ap",
        participante="99999999000191", carteira="CARTEIRA-01",
    ))

    corpo = json.loads(route.calls.last.request.content)
    assert corpo["documentoUsuarioFinalRecebedor"] == CNPJ_VALIDO
    assert corpo["documentoTitular"] == CPF_VALIDO
    assert corpo["dataInicio"] == "2026-09-01"
    assert corpo["dataFim"] == "2026-09-30"
    assert corpo["tipoAvaliacao"] == "avaliacao_agenda_basica_ap"
    assert corpo["participante"] == "99999999000191"
    assert corpo["carteira"] == "CARTEIRA-01"


@respx.mock
def test_consultar_agenda_erro_de_conexao_e_retentavel_e_fecha_consulta_em_erro():
    respx.post(URL_CONSULTAR).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(client.CercConsultaRetentavelError):
        client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    db = get_db(FINANCIADOR_TESTE)
    consultas = db.table("consulta_agenda").select("*").eq("filtro_ufr", CNPJ_VALIDO).execute().data
    assert len(consultas) == 1
    assert consultas[0]["status"] == "ERRO"
    assert consultas[0]["encerrada_em"] is not None

    requisicoes = db.table("cerc_requisicao").select("*").eq("recurso", "agenda_consultar").execute().data
    assert len(requisicoes) == 1
    assert requisicoes[0]["http_status"] is None


@respx.mock
def test_consultar_agenda_corpo_de_erro_nao_conforme_nao_quebra():
    respx.post(URL_CONSULTAR).mock(
        return_value=httpx.Response(500, content=b"<html>gateway error</html>", headers={"content-type": "text/html"})
    )

    with pytest.raises(client.CercConsultaRetentavelError):
        client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    db = get_db(FINANCIADOR_TESTE)
    requisicoes = db.table("cerc_requisicao").select("*").eq("recurso", "agenda_consultar").execute().data
    assert len(requisicoes) == 1
    assert requisicoes[0]["http_status"] == 500


@respx.mock
def test_consultar_agenda_codigo_105801_e_critico():
    respx.post(URL_CONSULTAR).mock(
        return_value=httpx.Response(403, json={"erros": [{"codigo": 105801, "mensagem": "acesso negado"}]})
    )
    with pytest.raises(client.CercConsultaCriticaError):
        client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())


@respx.mock
def test_consultar_agenda_persiste_trilha_de_compliance():
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc()))

    resultado = client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base(
        ator="analista@empresa.com", origem_ip="10.0.0.1",
        base_autorizativa={"tipo": "CONTRATO", "id": "ctr_1"},
    ))

    db = get_db(FINANCIADOR_TESTE)
    consulta = db.table("consulta_agenda").select("*").eq("id", resultado["consultaId"]).execute().data[0]
    assert consulta["ator"] == "analista@empresa.com"
    assert consulta["origem_ip"] == "10.0.0.1"
    assert consulta["base_autorizativa_tipo"] == "CONTRATO"
    assert consulta["base_autorizativa_id"] == "ctr_1"


@respx.mock
def test_consultar_agenda_usa_pagamentos_do_nivel_da_ur_quando_titular_nao_tem():
    resposta = _resposta_cerc()
    ur = resposta[0]["unidadesRecebiveis"][0]
    ur["pagamentos"] = [{
        "tipoInformacaoPagamento": 7,
        "domicilioPagamento": {"tipoConta": "CC"},
        "valorAPagar": 1000.0,
    }]
    ur["titulares"][0]["pagamentos"] = []
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=resposta))

    client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    db = get_db(FINANCIADOR_TESTE)
    pagamentos = repository._com_filtros(
        db.table("agenda_ur_pagamento").select("*"), CHAVE_UR_TESTE, repository._CHAVE_UR
    ).execute().data
    assert len(pagamentos) == 1
    assert pagamentos[0]["tipo_informacao_pagamento"] == "7"


@respx.mock
def test_consultar_agenda_isola_ur_malformada_sem_quebrar_o_lote():
    db = get_db(FINANCIADOR_TESTE)
    antes = db.table("agenda_ur_rejeitada").select("id").execute().data
    ids_antes = {r["id"] for r in antes}

    resposta = _resposta_cerc()
    ur_malformada = {"dataLiquidacao": "2026-09-21"}  # sem titulares/constituicao/etc — KeyError na tradução
    resposta[0]["unidadesRecebiveis"].append(ur_malformada)
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=resposta))

    resultado = client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    assert resultado["status"] == "COMPLETA"
    ur_boa = repository._com_filtros(
        db.table("agenda_ur").select("*"), CHAVE_UR_TESTE, repository._CHAVE_UR
    ).execute().data
    assert len(ur_boa) == 1  # a UR válida foi persistida mesmo com a malformada no mesmo lote

    depois = db.table("agenda_ur_rejeitada").select("*").execute().data
    novas = [r for r in depois if r["id"] not in ids_antes]
    assert len(novas) >= 1
    for r in novas:
        db.table("agenda_ur_rejeitada").delete().eq("id", r["id"]).execute()
