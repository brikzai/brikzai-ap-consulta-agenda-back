# agenda-service — Plan 03: Shared Data-Access Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the app a real way to talk to the database — `shared/cloudsql_client.py` (Supabase-style query builder over Cloud SQL), `shared/secrets.py`, and `shared/tenant_config.py` — copied verbatim from `ap-back-optin`'s current (already multi-tenant-retrofitted) versions, per house convention (code copied between sibling services, never a shared package).

**Architecture:** Three tightly-coupled modules that only make sense together (`cloudsql_client.get_db(financiador_id)` calls `tenant_config.get_tenant_config(financiador_id)`, which calls `secrets.get_secret(name)`) — one task, not three, since none of them is independently reviewable without the others already existing. Multi-tenancy is a database-per-financiador model: `get_db(financiador_id)` returns a cached `CloudSQLClient` wrapping a SQLAlchemy engine built from that tenant's Cloud SQL connection info, itself read from a per-tenant secret (`TENANT_{financiador_id}_CONFIG`, JSON).

**Tech Stack:** SQLAlchemy, pg8000, `cloud-sql-python-connector[pg8000]`, `google-cloud-secret-manager` (all already in `requirements.txt` from Plan 01).

**Spec:** `docs/superpowers/specs/2026-08-24-agenda-service-design.md` (§3.1-3.2, §4, §6). Series: plan 3 of ~10.

**Depends on:** `2026-08-24-agenda-plan-01-scaffold.md` (repo layout), `2026-08-24-agenda-plan-02-schema.md` (real tables to round-trip against — this task's tests hit the real `agenda` database, using `dominio_arranjo` as the scratch table, same choice `ap-back-optin`'s own tests make).

## Global Constraints

- No Django ORM — this **is** the data access layer these three files provide; nothing here should import `django.db`.
- Secrets never committed; `.env` (already populated with a real `TENANT_12345678000199_CONFIG`, see below) stays git-ignored.
- These files are **copied, not shared as a package** between `ap-back-optin`, `ap-back-contratos`, and this service — each repo keeps its own copy, per already-established house convention. Do not add a dependency on either sibling repo.
- The dev/test tenant is `financiador_id = "12345678000199"` — the same fixed CNPJ `ap-back-optin` already uses for its own dev tenant (design doc §14), so `.env`'s `TENANT_12345678000199_CONFIG` (already present, pointing at the real `agenda` database) is the one these tests exercise against.

---

### Task 1: `shared/secrets.py`, `shared/tenant_config.py`, `shared/cloudsql_client.py`

**Files:**
- Create: `shared/__init__.py`
- Create: `shared/secrets.py`
- Create: `shared/tenant_config.py`
- Create: `shared/cloudsql_client.py`
- Test: `shared/tests/__init__.py`
- Test: `shared/tests/test_secrets.py`
- Test: `shared/tests/test_tenant_config.py`
- Test: `shared/tests/test_cloudsql_client.py`

**Interfaces:**
- Produces: `shared.secrets.get_secret(name: str) -> str`; `shared.tenant_config.get_tenant_config(financiador_id: str) -> dict`; `shared.cloudsql_client.get_db(financiador_id: str) -> CloudSQLClient`, where `CloudSQLClient.table(name).select()/.insert()/.update()/.delete()` each return a `QueryBuilder` chainable with `.eq()`/`.gte()`/`.lte()`/`.order()`/`.limit()`, terminated by `.execute() -> ExecuteResult` (`.data: list[dict]`, `.count: int | None`). Every later plan (04 auth, 05 upsert repository, 06 CERC client, 07 webhook, 08 file ingestion, 09 API) reads/writes the database exclusively through `get_db(financiador_id).table(...)`.

- [ ] **Step 1: Write `shared/__init__.py` (empty) and `shared/secrets.py`**

```python
"""Leitura de segredos — Secret Manager em produção/homolog, env var em dev local.

Dev local: sem GOOGLE_CLOUD_PROJECT setado, lê a env var com o mesmo nome do
segredo (ex.: CERC_CLIENT_SECRET no .env). Em produção/homolog, lê do Secret
Manager do projeto (versão "latest").
"""

import os


def get_secret(name: str) -> str:
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if not project:
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"Secret '{name}' não configurado (defina a env var localmente)")
        return value

    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{project}/secrets/{name}/versions/latest"
    response = client.access_secret_version(name=path)
    return response.payload.data.decode("utf-8")
```

- [ ] **Step 2: Write `shared/tenant_config.py`**

```python
"""Configuração por tenant (financiador) — multi-tenancy (design doc §3.2).

Um segredo por tenant (TENANT_{financiador_id}_CONFIG, JSON) via
shared.secrets.get_secret — dev local lê a env var de mesmo nome (sem
GOOGLE_CLOUD_PROJECT); produção/homolog lê do Secret Manager, um segredo
por tenant. Cacheado em memória por processo, sem TTL (mesma filosofia do
cache de token de services/cerc/token_provider.py, Plano 04).
"""
import json

from shared.secrets import get_secret

_cache: dict = {}


def get_tenant_config(financiador_id: str) -> dict:
    if financiador_id in _cache:
        return _cache[financiador_id]

    raw = get_secret(f"TENANT_{financiador_id}_CONFIG")
    config = json.loads(raw)
    _cache[financiador_id] = config
    return config
```

- [ ] **Step 3: Write `shared/cloudsql_client.py`**

```python
"""Cliente Cloud SQL — API estilo Supabase/PostgREST sobre SQLAlchemy.

    get_db(financiador_id).table("agenda_ur").select("*").eq("documento_ufr", "...").limit(10).execute()
    get_db(financiador_id).table("agenda_ur").insert({...}).execute()

Sem Django ORM (design doc §3.1): DATABASES={} no settings, todo acesso
passa por aqui. Um Cloud SQL (banco) por tenant/financiador — a config de
conexão vem de shared.tenant_config.get_tenant_config, e o CloudSQLClient
resultante é cacheado em memória por financiador_id.
"""

import json
import logging
import threading
from typing import Any, List, Optional

from shared.tenant_config import get_tenant_config

logger = logging.getLogger(__name__)


class ExecuteResult:
    def __init__(self, data=None, count: Optional[int] = None):
        self.data = data or []
        self.count = count


class QueryBuilder:
    def __init__(self, engine, table_name: str):
        self._engine = engine
        self._table = table_name
        self._select_fields = "*"
        self._count_mode: Optional[str] = None
        self._filters: List[tuple] = []
        self._order_by: List[tuple] = []
        self._limit_val: Optional[int] = None
        self._op = "select"
        self._insert_data = None
        self._update_data: Optional[dict] = None

    def select(self, fields: str = "*", count: Optional[str] = None) -> "QueryBuilder":
        self._select_fields = fields
        self._count_mode = count
        return self

    def eq(self, field: str, value: Any) -> "QueryBuilder":
        self._filters.append(("eq", field, value))
        return self

    def gte(self, field: str, value: Any) -> "QueryBuilder":
        self._filters.append(("gte", field, value))
        return self

    def lte(self, field: str, value: Any) -> "QueryBuilder":
        self._filters.append(("lte", field, value))
        return self

    def order(self, field: str, desc: bool = False) -> "QueryBuilder":
        self._order_by.append((field, desc))
        return self

    def limit(self, n: int) -> "QueryBuilder":
        self._limit_val = n
        return self

    def insert(self, data) -> "QueryBuilder":
        self._op = "insert"
        self._insert_data = data
        return self

    def update(self, data: dict) -> "QueryBuilder":
        self._op = "update"
        self._update_data = data
        return self

    def delete(self) -> "QueryBuilder":
        self._op = "delete"
        return self

    def execute(self) -> ExecuteResult:
        try:
            return {
                "select": self._exec_select,
                "insert": self._exec_insert,
                "update": self._exec_update,
                "delete": self._exec_delete,
            }[self._op]()
        except Exception:
            logger.exception("[CloudSQL] Erro em %s.%s", self._table, self._op)
            raise

    def _build_where(self):
        if not self._filters:
            return "", {}
        clauses, params = [], {}
        operadores = {"eq": "=", "gte": ">=", "lte": "<="}
        for i, (op, field, val) in enumerate(self._filters):
            pname = f"p{i}"
            clauses.append(f"{field} {operadores[op]} :{pname}")
            params[pname] = val
        return "WHERE " + " AND ".join(clauses), params

    @staticmethod
    def _serialize(v: Any) -> Any:
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False, default=str)
        return v

    @staticmethod
    def _deserialize_row(row: dict) -> dict:
        result = {}
        for k, v in row.items():
            if isinstance(v, str) and len(v) > 1 and v[0] in ("{", "["):
                try:
                    result[k] = json.loads(v)
                    continue
                except (json.JSONDecodeError, ValueError):
                    pass
            result[k] = v
        return result

    def _exec_select(self) -> ExecuteResult:
        from sqlalchemy import text

        where, params = self._build_where()
        with self._engine.connect() as conn:
            if self._count_mode == "exact":
                sql = f"SELECT COUNT(*) FROM {self._table} {where}"
                return ExecuteResult(data=[], count=conn.execute(text(sql), params).scalar())

            order_clause = ""
            if self._order_by:
                parts = [f"{f} {'DESC' if d else 'ASC'}" for f, d in self._order_by]
                order_clause = "ORDER BY " + ", ".join(parts)
            limit_clause = f"LIMIT {self._limit_val}" if self._limit_val else ""

            sql = f"SELECT {self._select_fields} FROM {self._table} {where} {order_clause} {limit_clause}"
            result = conn.execute(text(sql), params)
            return ExecuteResult(data=[self._deserialize_row(dict(r._mapping)) for r in result])

    def _exec_insert(self) -> ExecuteResult:
        from sqlalchemy import text

        rows = self._insert_data if isinstance(self._insert_data, list) else [self._insert_data]
        inserted = []
        with self._engine.begin() as conn:
            for row in rows:
                serialized = {k: self._serialize(v) for k, v in row.items()}
                cols = list(serialized.keys())
                placeholders = [f":{c}" for c in cols]
                sql = f"INSERT INTO {self._table} ({', '.join(cols)}) VALUES ({', '.join(placeholders)}) RETURNING *"
                result = conn.execute(text(sql), serialized)
                inserted.extend(self._deserialize_row(dict(r._mapping)) for r in result)
        return ExecuteResult(data=inserted)

    def _exec_update(self) -> ExecuteResult:
        from sqlalchemy import text

        serialized = {k: self._serialize(v) for k, v in self._update_data.items()}
        set_clause = ", ".join(f"{k} = :u_{k}" for k in serialized)
        params = {f"u_{k}": v for k, v in serialized.items()}
        where, where_params = self._build_where()
        params.update(where_params)
        sql = f"UPDATE {self._table} SET {set_clause} {where} RETURNING *"
        with self._engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return ExecuteResult(data=[self._deserialize_row(dict(r._mapping)) for r in result])

    def _exec_delete(self) -> ExecuteResult:
        from sqlalchemy import text

        where, params = self._build_where()
        sql = f"DELETE FROM {self._table} {where} RETURNING *"
        with self._engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return ExecuteResult(data=[self._deserialize_row(dict(r._mapping)) for r in result])


class CloudSQLClient:
    def __init__(self, engine):
        self._engine = engine

    def table(self, name: str) -> QueryBuilder:
        return QueryBuilder(self._engine, name)


def _create_engine(config: dict):
    import sqlalchemy
    from google.cloud.sql.connector import Connector, IPTypes

    connector = Connector()

    def getconn():
        return connector.connect(
            config["cloudsql_connection_name"],
            "pg8000",
            user=config["cloudsql_db_user"],
            password=config["cloudsql_db_password"],
            db=config["cloudsql_db_name"],
            ip_type=IPTypes.PUBLIC,
        )

    logger.info("[CloudSQL] Engine criado para tenant (connection=%s)", config["cloudsql_connection_name"])
    return sqlalchemy.create_engine(
        "postgresql+pg8000://", creator=getconn, pool_size=5, max_overflow=2, pool_timeout=30, pool_recycle=1800,
    )


_meta_lock = threading.Lock()
_locks: dict = {}
_clients: dict = {}


def _lock_for(financiador_id: str) -> threading.Lock:
    if financiador_id not in _locks:
        with _meta_lock:
            if financiador_id not in _locks:
                _locks[financiador_id] = threading.Lock()
    return _locks[financiador_id]


def get_db(financiador_id: str) -> CloudSQLClient:
    if financiador_id in _clients:
        return _clients[financiador_id]

    with _lock_for(financiador_id):
        if financiador_id in _clients:
            return _clients[financiador_id]

        config = get_tenant_config(financiador_id)
        engine = _create_engine(config)
        client = CloudSQLClient(engine)
        _clients[financiador_id] = client
        return client
```

- [ ] **Step 4: Write `shared/tests/__init__.py` (empty) and `shared/tests/test_secrets.py`**

```python
from shared.secrets import get_secret
import pytest


def test_get_secret_reads_env_var_when_no_gcp_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("MY_SECRET", "valor-local")
    assert get_secret("MY_SECRET") == "valor-local"


def test_get_secret_raises_when_missing_locally(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("NAO_EXISTE", raising=False)
    with pytest.raises(RuntimeError):
        get_secret("NAO_EXISTE")
```

- [ ] **Step 5: Write `shared/tests/test_tenant_config.py`**

```python
import json

import pytest

import shared.tenant_config as tenant_config_module


@pytest.fixture(autouse=True)
def _clear_cache():
    tenant_config_module._cache.clear()
    yield
    tenant_config_module._cache.clear()


CONFIG_EXEMPLO = {
    "cloudsql_connection_name": "proj:region:instance",
    "cloudsql_db_user": "app",
    "cloudsql_db_password": "senha",
    "cloudsql_db_name": "app",
    "cerc_client_id": "client-1",
    "cerc_client_secret": "segredo",
    "cerc_cnpj_solicitante": "12345678000199",
}


def test_get_tenant_config_le_e_parseia_json(monkeypatch):
    from shared.tenant_config import get_tenant_config

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("TENANT_12345678000199_CONFIG", json.dumps(CONFIG_EXEMPLO))

    assert get_tenant_config("12345678000199") == CONFIG_EXEMPLO


def test_get_tenant_config_usa_cache_sem_reler_env(monkeypatch):
    from shared.tenant_config import get_tenant_config

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("TENANT_99999999000191_CONFIG", json.dumps(CONFIG_EXEMPLO))

    primeira = get_tenant_config("99999999000191")
    monkeypatch.delenv("TENANT_99999999000191_CONFIG", raising=False)
    segunda = get_tenant_config("99999999000191")

    assert primeira == segunda


def test_get_tenant_config_propaga_erro_quando_segredo_ausente(monkeypatch):
    from shared.tenant_config import get_tenant_config

    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("TENANT_00000000000000_CONFIG", raising=False)

    with pytest.raises(RuntimeError):
        get_tenant_config("00000000000000")
```

Note: `test_get_tenant_config_le_e_parseia_json` sets `TENANT_12345678000199_CONFIG` via `monkeypatch`, which **overrides** the real `.env` value for the duration of that one test only — `monkeypatch` restores the original value afterward, so this does not corrupt the real dev-tenant config other tests (`test_cloudsql_client.py`) rely on. If you observe otherwise, stop and report it as a concern rather than working around it.

- [ ] **Step 6: Write `shared/tests/test_cloudsql_client.py`**

```python
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
```

- [ ] **Step 7: Run the full test suite**

Run: `pytest shared/ -v`
Expected: PASS on all tests in `shared/tests/test_secrets.py`, `shared/tests/test_tenant_config.py`, `shared/tests/test_cloudsql_client.py` (the `cloudsql_client` tests hit the real `agenda` Cloud SQL database via the `.env`-provided `TENANT_12345678000199_CONFIG` — this is expected and matches `ap-back-optin`'s own test strategy, no mocking of the database itself).

- [ ] **Step 8: Commit**

```bash
git add shared/
git commit -m "feat: shared data-access layer (cloudsql_client, tenant_config, secrets), copied from ap-back-optin"
```

---

## Self-Review Notes

- **Spec coverage:** design doc §3.1 (no ORM, `CloudSQLClient`), §3.2 (multi-tenancy via `TENANT_{financiador_id}_CONFIG`), §4 (`shared/` folder), §6 (`get_db(financiador_id)` / `get_cerc_token(financiador_id)` usage pattern — the CERC-token half is Plan 04, not this plan) → fully covered for what this plan owns.
- **Placeholder scan:** none — every step has runnable code, copied verbatim from `ap-back-optin`'s current (already multi-tenant) files, confirmed identical by the controller immediately before writing this plan.
- **Type consistency:** `get_db(financiador_id: str) -> CloudSQLClient` and `CloudSQLClient.table(name).select/insert/update/delete().execute() -> ExecuteResult` are the exact names/signatures every later plan (04-09) will call. `get_tenant_config(financiador_id: str) -> dict` with keys `cloudsql_connection_name`, `cloudsql_db_user`, `cloudsql_db_password`, `cloudsql_db_name`, `cerc_client_id`, `cerc_client_secret`, `cerc_cnpj_solicitante` is the exact shape Plan 04's `token_provider.py` will read from.
- **Known, deliberate gap (design doc §15 risk 1, updated):** this `QueryBuilder` has no `upsert()` method. `ap-back-contratos` added one, but unconditionally (no `WHERE` clause), which does not implement this service's frescor-precedence upsert rule (design doc §8: `WEBHOOK > SINCRONO > ARQUIVO`, and "more recent always wins regardless of origin"). Plan 05 (upsert repository) must decide explicitly whether to extend this file with a conditional `upsert()` or keep a `SELECT` + compare in Python — not this plan's decision to make.

**Next:** `2026-08-24-agenda-plan-04-auth.md` (`shared/jwt_auth.py` + `services/cerc/token_provider.py`, copied from `ap-back-optin`).
