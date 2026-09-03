#!/usr/bin/env python
"""Aplica um arquivo .sql no Cloud SQL real deste serviço.

Uso: python scripts/apply_schema.py sql/schema/02-agenda-schema-fixes.sql

Não há Docker/Postgres local nesta máquina — este é o único mecanismo de
aplicar schema, tanto em dev quanto (futuramente) em homolog/produção. Usa
o mesmo caminho de conexão que o app em produção usará: Cloud SQL Python
Connector + SQLAlchemy (ver design doc §5). Lê CLOUDSQL_CONNECTION_NAME,
CLOUDSQL_DB_USER, CLOUDSQL_DB_PASSWORD, CLOUDSQL_DB_NAME do .env local.

Statements são separados por ";" — não usar ";" dentro de strings/valores
nos arquivos de schema aplicados por este script (nem blocos $$...$$; se um
dia precisar de trigger/function, troque o split por sqlparse.split()).

Idempotência: se a tabela schema_aplicado já existir (criada por
02-agenda-schema-fixes.sql), cada arquivo só é aplicado uma vez —
reaplicar um arquivo com o mesmo conteúdo é um no-op; reaplicar um arquivo
cujo conteúdo mudou é um erro (a fonte da verdade é o arquivo em disco, não
o banco — um checksum diferente indica que alguém editou um arquivo já
aplicado em vez de criar um novo numerado).
"""

import hashlib
import os
import sys

import sqlalchemy
from dotenv import load_dotenv
from google.cloud.sql.connector import Connector, IPTypes

load_dotenv()


def _create_engine():
    connector = Connector()

    def getconn():
        return connector.connect(
            os.environ["CLOUDSQL_CONNECTION_NAME"],
            "pg8000",
            user=os.environ["CLOUDSQL_DB_USER"],
            password=os.environ["CLOUDSQL_DB_PASSWORD"],
            db=os.environ["CLOUDSQL_DB_NAME"],
            ip_type=IPTypes.PUBLIC,
        )

    engine = sqlalchemy.create_engine("postgresql+pg8000://", creator=getconn)
    return engine, connector


def _ledger_table_exists(conn) -> bool:
    # Sem qualificar com o schema: resolve pelo search_path da conexão —
    # cai em "public" numa instância dedicada, ou no schema do tenant
    # (ex.: "agenda") quando o usuário de conexão tem search_path próprio,
    # caso de instância Cloud SQL compartilhada com outro serviço irmão.
    result = conn.exec_driver_sql(
        "SELECT to_regclass('schema_aplicado') IS NOT NULL"
    )
    return bool(result.scalar())


def _already_applied(conn, arquivo: str, checksum: str) -> bool:
    if not _ledger_table_exists(conn):
        return False
    result = conn.exec_driver_sql(
        "SELECT checksum FROM schema_aplicado WHERE arquivo = %s", (arquivo,)
    )
    row = result.fetchone()
    if row is None:
        return False
    if row[0] != checksum:
        raise RuntimeError(
            f"{arquivo} já foi aplicado com um checksum diferente — "
            "arquivo já aplicado foi editado. Crie um novo arquivo numerado "
            "em vez de editar um já aplicado."
        )
    return True


def apply_sql_file(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        sql = f.read()
    checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()

    statements = [s.strip() for s in sql.split(";") if s.strip()]

    engine, connector = _create_engine()
    try:
        print(f"Aplicando em: {os.environ['CLOUDSQL_CONNECTION_NAME']} (banco {os.environ['CLOUDSQL_DB_NAME']})")
        with engine.begin() as conn:
            if _already_applied(conn, path, checksum):
                print(f"{path}: já aplicado (checksum igual), pulando.")
                return 0

            for statement in statements:
                conn.exec_driver_sql(statement)

            if _ledger_table_exists(conn):
                conn.exec_driver_sql(
                    "INSERT INTO schema_aplicado (arquivo, checksum) VALUES (%s, %s) "
                    "ON CONFLICT (arquivo) DO UPDATE SET checksum = EXCLUDED.checksum, aplicado_em = now()",
                    (path, checksum),
                )
    finally:
        connector.close()
        engine.dispose()

    return len(statements)


def main():
    if len(sys.argv) != 2:
        print("uso: python scripts/apply_schema.py <arquivo.sql>")
        sys.exit(1)

    count = apply_sql_file(sys.argv[1])
    if count:
        print(f"Aplicado {sys.argv[1]}: {count} statement(s).")


if __name__ == "__main__":
    main()
