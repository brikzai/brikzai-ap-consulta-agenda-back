# shared/tests/test_cloudsql_client.py
from dotenv import load_dotenv
load_dotenv()

import os
import threading
import time

import pytest

FINANCIADOR_TESTE = "12345678000199"
FINANCIADOR_TESTE_2 = "99999999000191"
FINANCIADOR_TESTE_3 = "11111111000100"

from shared.cloudsql_client import get_db  # noqa: E402
import shared.cloudsql_client as cloudsql_client_module  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_dominio_arranjo():
    db = get_db(FINANCIADOR_TESTE)
    db.table("dominio_arranjo").delete().eq("codigo", "VCC").execute()
    yield
    db.table("dominio_arranjo").delete().eq("codigo", "VCC").execute()


def test_insert_select_update_delete_round_trip():
    db = get_db(FINANCIADOR_TESTE)

    inserted = db.table("dominio_arranjo").insert({
        "codigo": "VCC",
        "descricao": "Visa Crédito",
        "ativo": True,
        "atualizado_em": "2026-08-19T00:00:00-03:00",
    }).execute()
    assert inserted.data[0]["codigo"] == "VCC"

    found = db.table("dominio_arranjo").select("*").eq("codigo", "VCC").execute()
    assert len(found.data) == 1
    assert found.data[0]["ativo"] is True

    updated = db.table("dominio_arranjo").update({"ativo": False}).eq("codigo", "VCC").execute()
    assert updated.data[0]["ativo"] is False

    deleted = db.table("dominio_arranjo").delete().eq("codigo", "VCC").execute()
    assert len(deleted.data) == 1

    empty = db.table("dominio_arranjo").select("*").eq("codigo", "VCC").execute()
    assert empty.data == []


def test_get_db_cacheia_por_financiador_id(monkeypatch):
    cloudsql_client_module._clients.clear()
    # Aponta o "segundo tenant" para a MESMA config do tenant de teste — o
    # objetivo aqui é provar que o cache é chaveado por financiador_id (dois
    # tenants diferentes nunca compartilham o mesmo CloudSQLClient), não
    # provisionar um segundo Cloud SQL real só para este teste.
    monkeypatch.setenv(
        f"TENANT_{FINANCIADOR_TESTE_2}_CONFIG",
        os.environ[f"TENANT_{FINANCIADOR_TESTE}_CONFIG"],
    )

    db1a = get_db(FINANCIADOR_TESTE)
    db1b = get_db(FINANCIADOR_TESTE)
    db2 = get_db(FINANCIADOR_TESTE_2)

    assert db1a is db1b
    assert db1a is not db2

    cloudsql_client_module._clients.pop(FINANCIADOR_TESTE_2, None)


def test_get_db_single_flight_on_concurrent_first_access(monkeypatch):
    # Duas (aqui, dez) threads tentando get_db() pela primeira vez para o
    # MESMO financiador_id ainda não cacheado, ao mesmo tempo. Sem o lock
    # por-tenant, cada uma chamaria _create_engine (engine + connector reais)
    # e a perdedora vazaria um pool de conexões nunca fechado. Aqui trocamos
    # _create_engine por um fake lento para alargar a janela de corrida e
    # contamos quantas vezes ele é de fato chamado.
    cloudsql_client_module._clients.pop(FINANCIADOR_TESTE_3, None)
    cloudsql_client_module._locks.pop(FINANCIADOR_TESTE_3, None)
    monkeypatch.setenv(
        f"TENANT_{FINANCIADOR_TESTE_3}_CONFIG",
        os.environ[f"TENANT_{FINANCIADOR_TESTE}_CONFIG"],
    )

    call_count = 0
    count_lock = threading.Lock()

    def _slow_fake_engine(config):
        nonlocal call_count
        with count_lock:
            call_count += 1
        time.sleep(0.05)  # alarga a janela pra forçar a corrida
        return object()

    monkeypatch.setattr(cloudsql_client_module, "_create_engine", _slow_fake_engine)

    results = []

    def _call():
        results.append(get_db(FINANCIADOR_TESTE_3))

    threads = [threading.Thread(target=_call) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert call_count == 1  # engine construído uma única vez
    assert len({id(r) for r in results}) == 1  # todas as threads recebem o mesmo client

    cloudsql_client_module._clients.pop(FINANCIADOR_TESTE_3, None)
    cloudsql_client_module._locks.pop(FINANCIADOR_TESTE_3, None)


def test_gte_lte_filters_range_query():
    db = get_db(FINANCIADOR_TESTE)
    db.table("dominio_arranjo").delete().eq("codigo", "GTE1").execute()
    db.table("dominio_arranjo").delete().eq("codigo", "GTE2").execute()
    try:
        db.table("dominio_arranjo").insert({
            "codigo": "GTE1", "descricao": "A", "ativo": True,
            "atualizado_em": "2026-01-01T00:00:00-03:00",
        }).execute()
        db.table("dominio_arranjo").insert({
            "codigo": "GTE2", "descricao": "B", "ativo": True,
            "atualizado_em": "2026-06-01T00:00:00-03:00",
        }).execute()

        recentes = db.table("dominio_arranjo").select("*").gte(
            "atualizado_em", "2026-03-01T00:00:00-03:00"
        ).eq("ativo", True).execute()
        codigos = {r["codigo"] for r in recentes.data}
        assert "GTE2" in codigos and "GTE1" not in codigos

        antigos = db.table("dominio_arranjo").select("*").lte(
            "atualizado_em", "2026-03-01T00:00:00-03:00"
        ).eq("ativo", True).execute()
        codigos_antigos = {r["codigo"] for r in antigos.data}
        assert "GTE1" in codigos_antigos and "GTE2" not in codigos_antigos
    finally:
        db.table("dominio_arranjo").delete().eq("codigo", "GTE1").execute()
        db.table("dominio_arranjo").delete().eq("codigo", "GTE2").execute()


def test_array_column_round_trips_as_native_postgres_array():
    db = get_db(FINANCIADOR_TESTE)
    db.table("politica_consulta").delete().eq("motivo", "TESTE_ARRAY").execute()
    try:
        inserted = db.table("politica_consulta").insert({
            "id": "01J8ZKARRAYTEST0000000001",
            "motivo": "TESTE_ARRAY",
            "modos_permitidos": ["BATCH", "ONLINE"],
        }).execute()
        assert inserted.data[0]["modos_permitidos"] == ["BATCH", "ONLINE"]

        found = db.table("politica_consulta").select("*").eq("motivo", "TESTE_ARRAY").execute()
        assert found.data[0]["modos_permitidos"] == ["BATCH", "ONLINE"]
    finally:
        db.table("politica_consulta").delete().eq("motivo", "TESTE_ARRAY").execute()


def test_unfiltered_delete_raises_without_allow_all():
    db = get_db(FINANCIADOR_TESTE)
    with pytest.raises(ValueError):
        db.table("dominio_arranjo").delete().execute()


def test_unfiltered_delete_allowed_with_allow_all(monkeypatch):
    db = get_db(FINANCIADOR_TESTE)
    db.table("dominio_arranjo").insert({
        "codigo": "ZZZ_ALLOWALL", "descricao": "teste", "ativo": True,
        "atualizado_em": "2026-08-19T00:00:00-03:00",
    }).execute()
    # Não realmente deleta a tabela inteira — só prova que allow_all=True passa
    # pela guarda; a limpeza abaixo usa .eq() como qualquer outro teste.
    result = db.table("dominio_arranjo").delete(allow_all=True).eq("codigo", "ZZZ_ALLOWALL").execute()
    assert len(result.data) == 1


def test_invalid_table_name_rejected():
    with pytest.raises(ValueError):
        get_db(FINANCIADOR_TESTE).table("agenda_ur; DROP TABLE agenda_ur")


def test_invalid_column_name_rejected_in_filter():
    db = get_db(FINANCIADOR_TESTE)
    with pytest.raises(ValueError):
        db.table("dominio_arranjo").select("*").eq("codigo = '1'; DROP TABLE dominio_arranjo; --", "x").execute()
