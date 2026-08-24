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
