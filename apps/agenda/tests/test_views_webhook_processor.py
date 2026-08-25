import base64
import json
from datetime import date, datetime, timezone

import pytest
from django.test import Client
from ulid import ULID

from apps.agenda import repository, views
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
URL = "/api/v1/webhooks/agenda/processar"
UFR_TESTE = "44444444000144"

CHAVE_UR_TESTE = {
    "data_liquidacao": "2026-09-25",
    "entidade_registradora": "22246686000196",
    "cnpj_credenciadora": "36216798000150",
    "documento_ufr": UFR_TESTE,
    "documento_titular": UFR_TESTE,
    "codigo_arranjo": "VCC",
}


def _push_envelope(webhook_inbox_id, financiador_id=FINANCIADOR_TESTE):
    dados = json.dumps({"webhook_inbox_id": webhook_inbox_id, "financiador_id": financiador_id}).encode()
    return json.dumps({
        "message": {"data": base64.b64encode(dados).decode(), "messageId": "msg-1", "publishTime": "2026-08-25T12:00:00Z"},
        "subscription": "projects/registradora-506000/subscriptions/agenda-webhook-inbox-sub",
    })


def _evento_agenda(**overrides):
    base = {
        "entidadeRegistradora": CHAVE_UR_TESTE["entidade_registradora"],
        "instituicaoCredenciadora": CHAVE_UR_TESTE["cnpj_credenciadora"],
        "documentoUsuarioFinalRecebedor": UFR_TESTE,
        "codigoArranjoPagamento": "VCC",
        "documentoTitular": UFR_TESTE,
        "dataLiquidacao": CHAVE_UR_TESTE["data_liquidacao"],
        "constituicao": "1",
        "valorConstituidoTotal": 1000.0,
        "valorTotalUR": 1000.0,
        "dataHoraUltimaAtualizacao": "2026-08-25T12:00:00.000Z",
        "pagamentos": [],
    }
    base.update(overrides)
    return base


def _payload(evento, tipo_evento="agenda"):
    return {"tipoEvento": tipo_evento, "dataHoraEvento": "2026-08-25T12:00:00.000Z", "evento": evento}


def _criar_webhook_inbox(payload, processado_em=None):
    webhook_id = str(ULID())
    get_db(FINANCIADOR_TESTE).table("webhook_inbox").insert({
        "id": webhook_id,
        "tipo_evento": payload["tipoEvento"],
        "data_hora_evento": datetime.fromisoformat(payload["dataHoraEvento"].replace("Z", "+00:00")),
        "payload": payload,
        "hash_dedupe": webhook_id,
        "processado_em": processado_em,
    }).execute()
    return webhook_id


def _criar_consulta(id_, **overrides):
    base = {
        "id": id_, "modo": "ONLINE", "status": "PARCIAL",
        "filtro_ufr": UFR_TESTE, "filtro_titular": None,
        "filtro_credenciadoras": ["99T"], "filtro_arranjos": ["99T"],
        "filtro_data_inicio": date(2026, 9, 1), "filtro_data_fim": date(2026, 9, 30),
        "base_autorizativa_tipo": "OPTIN", "base_autorizativa_id": "opt_1",
        "motivo": "TESTE-PROCESSOR", "ator": "teste@teste.com",
        "qtd_urs_sincrono": 0, "qtd_urs_webhook": 0,
    }
    base.update(overrides)
    get_db(FINANCIADOR_TESTE).table("consulta_agenda").insert(base).execute()
    return id_


def _limpar(webhook_inbox_id=None, consulta_ids=()):
    db = get_db(FINANCIADOR_TESTE)
    parcial = {"data_liquidacao": CHAVE_UR_TESTE["data_liquidacao"], "entidade_registradora": CHAVE_UR_TESTE["entidade_registradora"]}
    campos_parciais = ("data_liquidacao", "entidade_registradora")
    repository._com_filtros(db.table("agenda_ur_evento").delete(), parcial, campos_parciais).execute()
    repository._com_filtros(db.table("agenda_ur_pagamento").delete(), parcial, campos_parciais).execute()
    repository._com_filtros(db.table("agenda_ur").delete(), parcial, campos_parciais).execute()
    for consulta_id in consulta_ids:
        db.table("consulta_agenda_ur").delete().eq("consulta_id", consulta_id).execute()
        db.table("consulta_agenda").delete().eq("id", consulta_id).execute()
    db.table("agenda_ur_orfa").delete().eq("payload", None).execute()  # no-op seguro; órfãos são limpos por id abaixo
    if webhook_inbox_id:
        db.table("webhook_inbox").delete().eq("id", webhook_inbox_id).execute()


@pytest.fixture(autouse=True)
def _oidc_ok(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: True)


def test_processor_sem_oidc_retorna_401(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: False)
    response = Client().post(URL, data=_push_envelope("qualquer-id"), content_type="application/json")
    assert response.status_code == 401


def test_processor_envelope_pubsub_malformado_retorna_400():
    response = Client().post(URL, data="isto nao e json", content_type="text/plain")
    assert response.status_code == 400


def test_processor_webhook_inbox_nao_encontrado_retorna_404():
    response = Client().post(URL, data=_push_envelope("id-inexistente"), content_type="application/json")
    assert response.status_code == 404


def test_processor_ja_processado_e_idempotente():
    webhook_id = _criar_webhook_inbox(_payload(_evento_agenda()), processado_em=datetime(2026, 8, 25, 12, 5, tzinfo=timezone.utc))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204
    finally:
        _limpar(webhook_inbox_id=webhook_id)


def test_processor_tipo_evento_diferente_de_agenda_e_ignorado_mas_marcado_processado():
    webhook_id = _criar_webhook_inbox(_payload({"algumCampo": "valor"}, tipo_evento="contrato"))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        linha = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("id", webhook_id).execute()
        assert linha.data[0]["processado_em"] is not None
    finally:
        _limpar(webhook_inbox_id=webhook_id)


def test_processor_sem_consulta_casada_vai_para_orfa():
    webhook_id = _criar_webhook_inbox(_payload(_evento_agenda()))
    db = get_db(FINANCIADOR_TESTE)
    antes = db.table("agenda_ur_orfa").select("id").execute().data
    ids_antes = {r["id"] for r in antes}
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        depois = db.table("agenda_ur_orfa").select("*").execute().data
        novas = [r for r in depois if r["id"] not in ids_antes]
        assert len(novas) == 1
        for r in novas:
            db.table("agenda_ur_orfa").delete().eq("id", r["id"]).execute()

        ur = repository._com_filtros(db.table("agenda_ur").select("*"), CHAVE_UR_TESTE, repository._CHAVE_UR).execute().data
        assert ur == []  # órfã não persiste em agenda_ur
    finally:
        _limpar(webhook_inbox_id=webhook_id)


def test_processor_com_consulta_casada_persiste_ur_e_vincula():
    consulta_id = _criar_consulta("proc-1")
    webhook_id = _criar_webhook_inbox(_payload(_evento_agenda()))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        db = get_db(FINANCIADOR_TESTE)
        ur = repository._com_filtros(db.table("agenda_ur").select("*"), CHAVE_UR_TESTE, repository._CHAVE_UR).execute().data
        assert len(ur) == 1
        assert ur[0]["origem"] == "WEBHOOK"

        vinculo = db.table("consulta_agenda_ur").select("*").eq("consulta_id", consulta_id).execute().data
        assert len(vinculo) == 1
        assert vinculo[0]["origem"] == "WEBHOOK"

        consulta = db.table("consulta_agenda").select("*").eq("id", consulta_id).execute().data[0]
        assert consulta["qtd_urs_webhook"] == 1
        assert consulta["ultima_ur_em"] is not None
    finally:
        _limpar(webhook_inbox_id=webhook_id, consulta_ids=[consulta_id])


def test_processor_casa_com_multiplas_consultas():
    consulta_a = _criar_consulta("proc-2a")
    consulta_b = _criar_consulta("proc-2b", motivo="OUTRO-MOTIVO")
    webhook_id = _criar_webhook_inbox(_payload(_evento_agenda()))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        db = get_db(FINANCIADOR_TESTE)
        for consulta_id in (consulta_a, consulta_b):
            vinculo = db.table("consulta_agenda_ur").select("*").eq("consulta_id", consulta_id).execute().data
            assert len(vinculo) == 1

        ur = repository._com_filtros(db.table("agenda_ur").select("*"), CHAVE_UR_TESTE, repository._CHAVE_UR).execute().data
        assert len(ur) == 1  # persistido uma única vez, mesmo casando duas consultas
    finally:
        _limpar(webhook_inbox_id=webhook_id, consulta_ids=[consulta_a, consulta_b])
