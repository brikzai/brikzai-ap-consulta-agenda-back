"""Testes do mecanismo de transação explícita de CloudSQLClient — a correção
para o achado "upsert_agenda_ur não é atômico" (cada QueryBuilder.execute()
abria sua própria conexão/transação, então uma falha no meio de um upsert
deixava escrita parcial). Usa SQLite em memória (não o Cloud SQL real, que
não está acessível neste ambiente) só para provar o MECANISMO — reuso de
conexão dentro de `db.transaction()` e rollback atômico em exceção — com uma
tabela mínima independente do schema real de agenda_ur."""
import pytest
import sqlalchemy
from sqlalchemy.pool import StaticPool

from shared.cloudsql_client import CloudSQLClient


@pytest.fixture
def engine():
    eng = sqlalchemy.create_engine(
        "sqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    with eng.begin() as conn:
        conn.execute(sqlalchemy.text("CREATE TABLE item (id INTEGER PRIMARY KEY, nome TEXT NOT NULL)"))
    return eng


def test_operacoes_dentro_de_transaction_sao_commitadas_juntas(engine):
    db = CloudSQLClient(engine)

    with db.transaction() as tx:
        tx.table("item").insert({"id": 1, "nome": "a"}).execute()
        tx.table("item").insert({"id": 2, "nome": "b"}).execute()

    linhas = db.table("item").select("*").execute().data
    assert {l["id"] for l in linhas} == {1, 2}


def test_excecao_dentro_de_transaction_desfaz_todas_as_escritas(engine):
    """O caso que motivou a correção: um upsert com múltiplas escritas (cabeçalho,
    eventos, pagamentos) não pode deixar rastro parcial quando falha no meio."""
    db = CloudSQLClient(engine)

    with pytest.raises(RuntimeError, match="falha no meio"):
        with db.transaction() as tx:
            tx.table("item").insert({"id": 1, "nome": "a"}).execute()
            raise RuntimeError("falha no meio da transação")

    linhas = db.table("item").select("*").execute().data
    assert linhas == []


def test_select_dentro_de_transaction_ve_escrita_ainda_nao_commitada(engine):
    db = CloudSQLClient(engine)

    with db.transaction() as tx:
        tx.table("item").insert({"id": 1, "nome": "a"}).execute()
        vistos = tx.table("item").select("*").eq("id", 1).execute().data
        assert len(vistos) == 1  # mesma conexão/transação — enxerga a própria escrita não commitada
