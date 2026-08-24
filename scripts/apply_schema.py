#!/usr/bin/env python
"""Aplica um arquivo .sql no Cloud SQL real deste serviço.

Uso: python scripts/apply_schema.py sql/schema/01-agenda-schema.sql

Não há Docker/Postgres local nesta máquina — este é o único mecanismo de
aplicar schema, tanto em dev quanto (futuramente) em homolog/produção. Usa
o mesmo caminho de conexão que o app em produção usará: Cloud SQL Python
Connector + SQLAlchemy (ver design doc §5). Lê CLOUDSQL_CONNECTION_NAME,
CLOUDSQL_DB_USER, CLOUDSQL_DB_PASSWORD, CLOUDSQL_DB_NAME do .env local.

Statements são separados por ";" — não usar ";" dentro de strings/valores
nos arquivos de schema aplicados por este script.
"""

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


def apply_sql_file(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        sql = f.read()

    statements = [s.strip() for s in sql.split(";") if s.strip()]

    engine, connector = _create_engine()
    try:
        with engine.begin() as conn:
            for statement in statements:
                conn.execute(sqlalchemy.text(statement))
    finally:
        connector.close()

    return len(statements)


def main():
    if len(sys.argv) != 2:
        print("uso: python scripts/apply_schema.py <arquivo.sql>")
        sys.exit(1)

    count = apply_sql_file(sys.argv[1])
    print(f"Aplicado {sys.argv[1]}: {count} statement(s).")


if __name__ == "__main__":
    main()
