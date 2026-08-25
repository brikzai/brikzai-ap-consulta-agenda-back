from datetime import date, datetime, timezone

import pytest

from apps.agenda.correlacao import encontrar_consultas_candidatas
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
UFR_TESTE = "33333333000133"


def _consulta(id_, **overrides):
    base = {
        "id": id_,
        "modo": "ONLINE",
        "status": "PARCIAL",
        "filtro_ufr": UFR_TESTE,
        "filtro_titular": None,
        "filtro_credenciadoras": ["99T"],
        "filtro_arranjos": ["99T"],
        "filtro_data_inicio": date(2026, 9, 1),
        "filtro_data_fim": date(2026, 9, 30),
        "base_autorizativa_tipo": "OPTIN",
        "base_autorizativa_id": "opt_1",
        "motivo": "TESTE-CORRELACAO",
        "ator": "teste@teste.com",
    }
    base.update(overrides)
    return base


def _evento(**overrides):
    base = {
        "entidadeRegistradora": "22246686000196",
        "instituicaoCredenciadora": "36216798000150",
        "documentoUsuarioFinalRecebedor": UFR_TESTE,
        "codigoArranjoPagamento": "VCC",
        "documentoTitular": UFR_TESTE,
        "dataLiquidacao": "2026-09-20",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _limpar():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").delete().eq("filtro_ufr", UFR_TESTE).execute()
    yield
    db.table("consulta_agenda").delete().eq("filtro_ufr", UFR_TESTE).execute()


def test_casa_por_curinga_em_credenciadora_e_arranjo():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta("corr-1")).execute()

    casadas = encontrar_consultas_candidatas(FINANCIADOR_TESTE, _evento())

    assert [c["id"] for c in casadas] == ["corr-1"]


def test_nao_casa_credenciadora_especifica_diferente():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta("corr-2", filtro_credenciadoras=["11111111000100"])).execute()

    casadas = encontrar_consultas_candidatas(FINANCIADOR_TESTE, _evento())

    assert casadas == []


def test_nao_casa_fora_da_janela_de_datas():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta(
        "corr-3", filtro_data_inicio=date(2026, 1, 1), filtro_data_fim=date(2026, 1, 31),
    )).execute()

    casadas = encontrar_consultas_candidatas(FINANCIADOR_TESTE, _evento())

    assert casadas == []


def test_casa_multiplas_consultas_simultaneamente():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta("corr-4a")).execute()
    db.table("consulta_agenda").insert(_consulta("corr-4b", motivo="OUTRO-MOTIVO")).execute()

    casadas = encontrar_consultas_candidatas(FINANCIADOR_TESTE, _evento())

    assert {c["id"] for c in casadas} == {"corr-4a", "corr-4b"}


def test_ignora_consulta_batch_e_consulta_ja_completa():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta("corr-5", modo="BATCH")).execute()
    db.table("consulta_agenda").insert(_consulta("corr-6", status="COMPLETA")).execute()

    casadas = encontrar_consultas_candidatas(FINANCIADOR_TESTE, _evento())

    assert casadas == []


def test_filtro_titular_especifico_so_casa_o_mesmo_titular():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta("corr-7", filtro_titular="99999999000191")).execute()

    casadas = encontrar_consultas_candidatas(FINANCIADOR_TESTE, _evento(documentoTitular=UFR_TESTE))

    assert casadas == []
