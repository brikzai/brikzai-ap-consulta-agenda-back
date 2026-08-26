import csv
import io

import pytest
from django.test import Client

from apps.agenda import views
from apps.agenda.parser_ap005 import parse_nome_arquivo
from apps.agenda.repository import _CHAVE_UR, _com_filtros
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
NOME_ARQUIVO = "CERC-AP005_22223333_20260922_0000001_ret.csv"
URL = f"/api/v1/jobs/importar-ap005/{FINANCIADOR_TESTE}"

_CHAVE_TESTE = {
    "data_liquidacao": "2026-09-22",
    "entidade_registradora": "22246686000196",
    "cnpj_credenciadora": "01027058000191",
    "documento_ufr": "99999999000199",
    "documento_titular": "99999999000199",
    "codigo_arranjo": "VCC",
}


def _linha_valida() -> bytes:
    campos = [
        "REF-001", _CHAVE_TESTE["entidade_registradora"], _CHAVE_TESTE["cnpj_credenciadora"],
        _CHAVE_TESTE["documento_ufr"], _CHAVE_TESTE["codigo_arranjo"], _CHAVE_TESTE["data_liquidacao"],
        _CHAVE_TESTE["documento_titular"], "1", "1000.00", "0", "0",
        _CHAVE_TESTE["documento_titular"], "CC", "001", "00000000", "1234", "123456-7",
        "500.00", "", "", "", "", "", "6", "", "", "CTR-VIEW",
        "", "0", "1000.00", "2026-09-22T10:00:00Z",
    ]
    buffer = io.StringIO()
    csv.writer(buffer).writerow(campos)
    return buffer.getvalue().encode("utf-8")


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: True)
    db = get_db(FINANCIADOR_TESTE)
    meta = parse_nome_arquivo(NOME_ARQUIVO)

    def _limpar():
        _com_filtros(db.table("agenda_ur_pagamento").delete(), _CHAVE_TESTE, _CHAVE_UR).execute()
        _com_filtros(db.table("agenda_ur").delete(), _CHAVE_TESTE, _CHAVE_UR).execute()
        db.table("arquivo_agenda_processado").delete().eq("tipo_leiaute", meta["tipo_leiaute"]).eq(
            "ident_ic", meta["ident_ic"]).eq("data_req", meta["data_req"]).eq("seq", meta["seq"]).execute()

    _limpar()
    yield
    _limpar()


def test_sem_oidc_retorna_401(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: False)
    response = Client().post(URL, data=_linha_valida(), content_type="application/octet-stream")
    assert response.status_code == 401


def test_sem_header_nome_arquivo_retorna_400():
    response = Client().post(URL, data=_linha_valida(), content_type="application/octet-stream")
    assert response.status_code == 400


def test_nome_arquivo_invalido_retorna_400():
    response = Client().post(
        URL, data=_linha_valida(), content_type="application/octet-stream",
        HTTP_X_NOME_ARQUIVO="arquivo_qualquer.csv",
    )
    assert response.status_code == 400


def test_importa_arquivo_com_sucesso():
    response = Client().post(
        URL, data=_linha_valida(), content_type="application/octet-stream",
        HTTP_X_NOME_ARQUIVO=NOME_ARQUIVO,
    )
    assert response.status_code == 200
    corpo = response.json()
    assert corpo["linhas_ok"] == 1
    assert corpo["ja_processado"] is False

    db = get_db(FINANCIADOR_TESTE)
    ur = _com_filtros(db.table("agenda_ur").select("*"), _CHAVE_TESTE, _CHAVE_UR).execute().data
    assert len(ur) == 1
