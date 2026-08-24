# agenda-service — Plan 02: Fase-1 Schema on Cloud SQL — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The fase-1 agenda schema applied to a real Cloud SQL database, so every later plan has real tables to read/write against.

**Architecture:** Plain versioned SQL in `sql/schema/`, applied via a small standalone script (`scripts/apply_schema.py`) using the Cloud SQL Python Connector — the same connection path production uses. No migration framework, no domains, no partitioning (house convention — see design doc §2/§5). No local Docker/Postgres: this dev machine has no Docker installed; per an explicit decision with the user, this service reuses the existing `app-db` Cloud SQL instance (already hosting `ap-back-optin`'s dev tenant) rather than provisioning a new dedicated instance for now — a new database inside it, not a new instance.

**Tech Stack:** Cloud SQL for PostgreSQL 16, `cloud-sql-python-connector[pg8000]`, SQLAlchemy (all already in `requirements.txt` from Plan 01).

**Spec:** `docs/superpowers/specs/2026-08-24-agenda-service-design.md` (§5). Normative source for field names/business rules: `docs/specs/SPEC-03-consulta-de-agenda-ap005.md` §4.3, §5, §6, §9 (only the field semantics — not its DDL, superseded by the design doc §0). Series: plan 2 of ~10.

**Depends on:** `2026-08-24-agenda-plan-01-scaffold.md` (repo layout, `requirements.txt`).

## Global Constraints

- Money columns are `NUMERIC(18,2)`; **never** `float`/`double`.
- Tables excluded from this schema on purpose: `indicador_consistencia_agenda` (entra quando `tipoAvaliacao` for exposto pela API interna, fora da fase 1 — design doc §5) — criar essa tabela agora seria schema sem código que a use.
- No `CREATE SCHEMA`, no `CREATE DOMAIN`, no `PARTITION BY` — all tables live in the default `public` schema of the `agenda` database, plain `TEXT`/`TIMESTAMPTZ`/`NUMERIC` columns (design doc §2, YAGNI until real volume justifies otherwise).
- **The database already exists** — provisioned outside this plan (controller ran `gcloud sql databases create` + `gcloud sql users create` against the existing `app-db` instance), so this task does not create infrastructure, only applies schema to it:
  - Instance: `app-db`, project `registradora-506000`, region `us-east1`, Postgres 16, tier `db-f1-micro` (shared with `ap-back-optin`'s dev tenant database, `app` — a different database on the same instance, no data overlap).
  - Database: `agenda`. User: `agenda_app` (full privileges on this database only).
  - Connection name: `registradora-506000:us-east1:app-db`.
  - `.env` in the repo root (git-ignored) already has `CLOUDSQL_CONNECTION_NAME`, `CLOUDSQL_DB_USER`, `CLOUDSQL_DB_PASSWORD`, `CLOUDSQL_DB_NAME` filled in with real values pointing at `agenda_app`/`agenda`. Do not print or log the contents of `.env` in any report — treat the password as a secret even though you can read the file to use it.
- Google Application Default Credentials are already configured on this machine (`gcloud auth application-default login` already run) — the connector will authenticate with them automatically; no additional auth setup needed.

---

### Task 1: `sql/schema/01-agenda-schema.sql` + `scripts/apply_schema.py`, applied to Cloud SQL

**Files:**
- Create: `sql/schema/01-agenda-schema.sql`
- Create: `scripts/apply_schema.py`
- Create: `scripts/__init__.py` (empty — makes it an importable package, keeps `python scripts/apply_schema.py` working either way)

**Interfaces:**
- Produces: all fase-1 tables created in the real `agenda` database on `app-db`: `consulta_agenda`, `agenda_ur`, `agenda_ur_pagamento`, `consulta_agenda_ur`, `agenda_ur_orfa`, `agenda_ur_rejeitada`, `arquivo_agenda_processado`, `politica_consulta`, `cerc_requisicao`, `webhook_inbox`, `dominio_arranjo`. Every later plan's `cloudsql_client.table("...")` calls read/write these exact names.
- Produces: `scripts/apply_schema.py <path-to-sql-file>` — a reusable command for applying future `sql/schema/NN-*.sql` files to the real instance.

- [ ] **Step 1: Write `sql/schema/01-agenda-schema.sql`**

```sql
CREATE TABLE consulta_agenda (
  id                     TEXT PRIMARY KEY,
  modo                   TEXT NOT NULL,            -- ONLINE | BATCH
  status                 TEXT NOT NULL,            -- PARCIAL|COMPLETA|COMPLETA_COM_TIMEOUT|ERRO
  filtro_ufr             TEXT NOT NULL,
  filtro_titular         TEXT,
  filtro_credenciadoras  TEXT[] NOT NULL,
  filtro_arranjos        TEXT[] NOT NULL,
  filtro_data_inicio     DATE NOT NULL,
  filtro_data_fim        DATE NOT NULL,
  tipo_avaliacao         TEXT,
  carteira               TEXT,
  base_autorizativa_tipo TEXT NOT NULL,            -- OPTIN | CONTRATO
  base_autorizativa_id   TEXT NOT NULL,
  motivo                 TEXT NOT NULL,
  ator                   TEXT NOT NULL,
  origem_ip              TEXT,
  qtd_urs_sincrono       INT NOT NULL DEFAULT 0,
  qtd_urs_webhook        INT NOT NULL DEFAULT 0,
  iniciada_em            TIMESTAMPTZ NOT NULL DEFAULT now(),
  ultima_ur_em           TIMESTAMPTZ,
  encerrada_em           TIMESTAMPTZ
);
CREATE INDEX ON consulta_agenda (filtro_ufr, iniciada_em);
CREATE INDEX ON consulta_agenda (status);
CREATE INDEX ON consulta_agenda (filtro_ufr, iniciada_em, modo);

CREATE TABLE agenda_ur (
  entidade_registradora TEXT NOT NULL,
  cnpj_credenciadora    TEXT NOT NULL,
  documento_ufr         TEXT NOT NULL,
  documento_titular     TEXT NOT NULL,
  codigo_arranjo        TEXT NOT NULL,
  data_liquidacao       DATE NOT NULL,
  constituicao          TEXT NOT NULL,             -- 1 constituida | 2 fumaca
  valor_constituido_total           NUMERIC(18,2) NOT NULL,
  valor_constituido_antecipacao_pre NUMERIC(18,2) NOT NULL DEFAULT 0,
  valor_bloqueado        NUMERIC(18,2) NOT NULL DEFAULT 0,
  valor_livre            NUMERIC(18,2) NOT NULL DEFAULT 0,
  valor_total_ur         NUMERIC(18,2) NOT NULL,
  carteira               TEXT,
  data_hora_ultima_atualizacao TIMESTAMPTZ NOT NULL,
  origem                 TEXT NOT NULL,            -- SINCRONO | WEBHOOK | ARQUIVO
  origem_arquivo         TEXT,                      -- AP005 | AP005A | AP005B
  atualizado_em           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (data_liquidacao, entidade_registradora, cnpj_credenciadora,
               documento_ufr, documento_titular, codigo_arranjo)
);
CREATE INDEX ON agenda_ur (documento_ufr, data_liquidacao);
CREATE INDEX ON agenda_ur (data_hora_ultima_atualizacao);

CREATE TABLE agenda_ur_pagamento (
  data_liquidacao        DATE NOT NULL,
  entidade_registradora  TEXT NOT NULL,
  cnpj_credenciadora     TEXT NOT NULL,
  documento_ufr          TEXT NOT NULL,
  documento_titular      TEXT NOT NULL,
  codigo_arranjo         TEXT NOT NULL,
  tipo_informacao_pagamento TEXT NOT NULL,          -- 1..8
  indicador_efeitos_contrato TEXT NOT NULL DEFAULT '',
  identificador_cerc_contrato TEXT,
  regras_divisao          TEXT,
  valor_onerado           NUMERIC(18,2),
  valor_constituido_efeito NUMERIC(18,2),
  valor_a_pagar            NUMERIC(18,2),
  beneficiario             TEXT,
  data_liquidacao_efetiva  DATE,
  valor_liquidacao_efetiva NUMERIC(18,2),
  motivo_nao_pagamento     TEXT,                    -- 001 | 002 | 999
  domicilio                JSONB NOT NULL,
  atualizado_em            TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (data_liquidacao, entidade_registradora, cnpj_credenciadora,
               documento_ufr, documento_titular, codigo_arranjo,
               tipo_informacao_pagamento, indicador_efeitos_contrato)
);
CREATE INDEX ON agenda_ur_pagamento (identificador_cerc_contrato);

CREATE TABLE consulta_agenda_ur (
  consulta_id           TEXT NOT NULL REFERENCES consulta_agenda(id),
  entidade_registradora TEXT NOT NULL,
  cnpj_credenciadora    TEXT NOT NULL,
  documento_ufr         TEXT NOT NULL,
  documento_titular     TEXT NOT NULL,
  codigo_arranjo        TEXT NOT NULL,
  data_liquidacao       DATE NOT NULL,
  origem                TEXT NOT NULL,
  recebida_em           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (consulta_id, entidade_registradora, cnpj_credenciadora,
               documento_ufr, documento_titular, codigo_arranjo, data_liquidacao)
);
CREATE INDEX ON consulta_agenda_ur (consulta_id);

CREATE TABLE agenda_ur_orfa (
  id           BIGSERIAL PRIMARY KEY,
  payload      JSONB NOT NULL,
  recebida_em  TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolvida_em TIMESTAMPTZ
);

CREATE TABLE agenda_ur_rejeitada (
  id          BIGSERIAL PRIMARY KEY,
  origem      TEXT NOT NULL,
  arquivo     TEXT,
  linha       INT,
  conteudo    TEXT,
  motivo      TEXT NOT NULL,
  ocorrida_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE arquivo_agenda_processado (
  tipo_leiaute      TEXT NOT NULL,
  ident_ic          TEXT NOT NULL,
  data_req          DATE NOT NULL,
  seq               INT NOT NULL,
  linhas_lidas      BIGINT,
  linhas_ok         BIGINT,
  linhas_rejeitadas BIGINT,
  iniciado_em       TIMESTAMPTZ,
  concluido_em      TIMESTAMPTZ,
  PRIMARY KEY (tipo_leiaute, ident_ic, data_req, seq)
);

CREATE TABLE politica_consulta (
  id               TEXT PRIMARY KEY,
  motivo           TEXT NOT NULL,
  modos_permitidos TEXT[] NOT NULL,
  ativo            BOOLEAN NOT NULL DEFAULT true,
  criado_em        TIMESTAMPTZ NOT NULL DEFAULT now(),
  atualizado_em    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (motivo)
);

CREATE TABLE cerc_requisicao (
  id            TEXT PRIMARY KEY,
  recurso       TEXT NOT NULL,
  correlacao_id TEXT NOT NULL,
  http_status   INT,
  request_body  JSONB NOT NULL,
  response_body JSONB,
  tentativa     INT NOT NULL DEFAULT 1,
  criado_em     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE webhook_inbox (
  id               TEXT PRIMARY KEY,
  tipo_evento      TEXT NOT NULL,
  data_hora_evento TIMESTAMPTZ NOT NULL,
  payload          JSONB NOT NULL,
  hash_dedupe      TEXT NOT NULL UNIQUE,
  processado_em    TIMESTAMPTZ,
  erro             TEXT,
  recebido_em      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE dominio_arranjo (
  codigo        TEXT PRIMARY KEY,
  descricao     TEXT,
  ativo         BOOLEAN NOT NULL DEFAULT true,
  atualizado_em TIMESTAMPTZ NOT NULL
);
```

- [ ] **Step 2: Write `scripts/__init__.py`**

Empty file.

- [ ] **Step 3: Write `scripts/apply_schema.py`**

```python
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
```

- [ ] **Step 4: Apply it to the real instance and verify**

Run: `python scripts/apply_schema.py sql/schema/01-agenda-schema.sql`
Expected: `Aplicado sql/schema/01-agenda-schema.sql: N statement(s).` with no error (N is the number of `;`-separated statements in the file — count them, don't guess).

Then verify the tables actually landed. Run this inline check (uses the same connector setup, so a successful run here also re-confirms Step 4 worked, not just that the script exited 0):

```bash
python -c "
from scripts.apply_schema import _create_engine
import sqlalchemy

engine, connector = _create_engine()
try:
    with engine.connect() as conn:
        rows = conn.execute(sqlalchemy.text(
            \"SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1\"
        )).fetchall()
        for r in rows:
            print(r[0])
finally:
    connector.close()
"
```

Expected output (11 lines, alphabetical): `agenda_ur`, `agenda_ur_orfa`, `agenda_ur_pagamento`, `agenda_ur_rejeitada`, `arquivo_agenda_processado`, `cerc_requisicao`, `consulta_agenda`, `consulta_agenda_ur`, `dominio_arranjo`, `politica_consulta`, `webhook_inbox`.

- [ ] **Step 5: Commit**

```bash
git add sql/schema/01-agenda-schema.sql scripts/apply_schema.py scripts/__init__.py
git commit -m "feat: fase-1 agenda schema, applied to Cloud SQL via apply_schema.py"
```

Note: `.env` is git-ignored and already existed before this task (real Cloud SQL credentials) — do not add it, and do not paste its contents into your report or commit message.

---

## Self-Review Notes

- **Spec coverage:** SPEC-03 §4.3/§5/§6/§9 field names, scoped to the fase-1 subset (excludes `indicador_consistencia_agenda`, deferred per design doc §5) — fully covered. Applying to the shared `app-db` instance (a new database inside it, not a new instance) reflects the controller's decision with the user, recorded in design doc §1/§14.
- **Placeholder scan:** none.
- **Type consistency:** table/column names match the design doc §5/§7/§8 prose exactly — every later plan's `cloudsql_client.table("...")` calls must match these names. `cerc_requisicao`/`webhook_inbox`/`dominio_arranjo` match `ap-back-optin`'s and `ap-back-contratos`' schema shape (same design, separate database). `scripts/apply_schema.py`'s `_create_engine()` connector setup is the same shape Plan 03's `shared/cloudsql_client.py` will use in production — Plan 03 should not need to invent a different pattern, just wrap it in the query-builder API and add the per-tenant config lookup.

**Next:** `2026-08-24-agenda-plan-03-cloudsql-client.md` (data access wrapper, copied from `ap-back-optin`).
