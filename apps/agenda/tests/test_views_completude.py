from datetime import date, datetime, timedelta, timezone

import pytest
from django.test import Client

from apps.agenda import views
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
URL = "/api/v1/jobs/varrer-completude"
UFR_TESTE = "55555555000155"


def _consulta(id_, *, iniciada_em, ultima_ur_em=None, status="PARCIAL"):
    return {
        "id": id_, "modo": "ONLINE", "status": status,
        "filtro_ufr": UFR_TESTE, "filtro_credenciadoras": ["99T"], "filtro_arranjos": ["99T"],
        "filtro_data_inicio": date(2026, 9, 1), "filtro_data_fim": date(2026, 9, 30),
        "base_autorizativa_tipo": "OPTIN", "base_autorizativa_id": "opt_1",
        "motivo": "TESTE-COMPLETUDE", "ator": "teste@teste.com",
        "iniciada_em": iniciada_em, "ultima_ur_em": ultima_ur_em,
    }


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: True)
    monkeypatch.setattr(views, "_TENANTS_JOBS_PERIODICOS", [FINANCIADOR_TESTE])
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").delete().eq("filtro_ufr", UFR_TESTE).execute()
    yield
    db.table("consulta_agenda").delete().eq("filtro_ufr", UFR_TESTE).execute()


def test_varrer_completude_sem_oidc_retorna_401(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: False)
    response = Client().post(URL)
    assert response.status_code == 401


def test_fecha_como_completa_apos_quiet_period():
    agora = datetime.now(timezone.utc)
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta(
        "comp-1", iniciada_em=agora - timedelta(minutes=5), ultima_ur_em=agora - timedelta(seconds=100),
    )).execute()

    response = Client().post(URL)
    assert response.status_code == 200

    consulta = db.table("consulta_agenda").select("*").eq("id", "comp-1").execute().data[0]
    assert consulta["status"] == "COMPLETA"
    assert consulta["encerrada_em"] is not None


def test_fecha_como_timeout_apos_hard_timeout():
    agora = datetime.now(timezone.utc)
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta(
        "comp-2", iniciada_em=agora - timedelta(minutes=20), ultima_ur_em=agora - timedelta(seconds=10),
    )).execute()

    response = Client().post(URL)
    assert response.status_code == 200

    consulta = db.table("consulta_agenda").select("*").eq("id", "comp-2").execute().data[0]
    assert consulta["status"] == "COMPLETA_COM_TIMEOUT"


def test_nao_toca_consulta_ainda_dentro_do_quiet_period():
    agora = datetime.now(timezone.utc)
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta(
        "comp-3", iniciada_em=agora - timedelta(minutes=1), ultima_ur_em=agora - timedelta(seconds=10),
    )).execute()

    response = Client().post(URL)
    assert response.status_code == 200

    consulta = db.table("consulta_agenda").select("*").eq("id", "comp-3").execute().data[0]
    assert consulta["status"] == "PARCIAL"


def test_ignora_consulta_ja_completa():
    agora = datetime.now(timezone.utc)
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta(
        "comp-4", iniciada_em=agora - timedelta(minutes=20), status="COMPLETA",
    )).execute()

    response = Client().post(URL)
    assert response.status_code == 200

    consulta = db.table("consulta_agenda").select("*").eq("id", "comp-4").execute().data[0]
    assert consulta["status"] == "COMPLETA"
