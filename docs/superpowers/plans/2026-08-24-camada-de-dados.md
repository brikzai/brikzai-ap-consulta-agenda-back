# Camada de Dados (Plano 1/7) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Criar a fundação do `agenda-service` — projeto Django, schemas `cerc` e `agenda` no Postgres (DDL da SPEC 04), modelos Django somente-leitura e o repositório de upsert com a regra de precedência de frescor (WEBHOOK > SINCRONO > ARQUIVO).

**Architecture:** Migrations Django com `RunSQL` aplicam o DDL exato da SPEC 04 (schemas, domínios, funções, tabelas particionadas com partição `DEFAULT`). Modelos Django são `managed=False` — a fonte de verdade do schema é o SQL versionado, não o ORM. Escritas críticas passam por um repositório com SQL explícito, nunca `.save()`.

**Tech Stack:** Python 3.12, Django 5.2+ (usa `CompositePrimaryKey`, adicionado nesta versão, para as tabelas com chave natural composta), PostgreSQL 16 (local via Docker), psycopg2-binary, pytest + pytest-django.

**Spec:** `docs/superpowers/specs/2026-08-24-agenda-service-design.md` (seções 3, 5, 7); DDL de referência em `docs/specs/SPEC-04-modelo-de-dados.md` §3, §5.1, §5.4, §5.5.

## Global Constraints

- Dinheiro é `NUMERIC(18,2)` na aplicação (`DecimalField(max_digits=18, decimal_places=2)`). Nunca `float`/`double`. (SPEC 04 §1.1)
- Tempo é `TIMESTAMPTZ`; datas de negócio da CERC (`dataLiquidacao` etc.) são `DATE` puro, nunca convertidas para timestamp. (SPEC 04 §1.2)
- IDs técnicos são ULID em `TEXT` (26 caracteres), gerados na aplicação — nunca `SERIAL`/`BigAutoField` para agregados de negócio (`BigAutoField` só é aceitável em tabelas de log append-only como `agenda_ur_orfa`/`agenda_ur_rejeitada`). (SPEC 04 §1.3)
- Documentos (CPF/CNPJ) são `TEXT` normalizado (só dígitos, zero-padded), nunca com máscara. (SPEC 04 §1.4)
- Toda tabela de alto volume nasce particionada, com partição `DEFAULT` como rede de segurança. (SPEC 04 §1.6, §4.3)
- Todo modelo Django que mapeia uma tabela deste plano é `managed=False` — as migrations usam `RunSQL`, nunca o gerador automático do Django.
- Escritas de upsert usam SQL explícito via `django.db.connection`, nunca `.save()`/`.get_or_create()` do ORM.

---

### Task 1: Scaffolding do projeto Django + Postgres local

**Files:**
- Create: `requirements.txt`
- Create: `manage.py`
- Create: `agendaservice/__init__.py`
- Create: `agendaservice/settings.py`
- Create: `agendaservice/urls.py`
- Create: `docker-compose.yml`
- Create: `.env.example`
- Create: `pytest.ini`
- Create: `.gitignore`

**Interfaces:**
- Produces: `agendaservice.settings` (`DJANGO_SETTINGS_MODULE`), variáveis de ambiente `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT` consumidas por todas as tasks seguintes.

- [ ] **Step 1: Criar `requirements.txt`**

```text
Django>=5.2,<6
psycopg2-binary>=2.9,<3
python-dotenv>=1.0,<2
pytest>=8.0,<9
pytest-django>=4.8,<5
```

- [ ] **Step 2: Criar `docker-compose.yml` com Postgres local**

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: agenda_service
      POSTGRES_USER: agenda_service
      POSTGRES_PASSWORD: agenda_service
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data

volumes:
  postgres_data:
```

- [ ] **Step 3: Criar `.env.example`**

```text
DJANGO_SECRET_KEY=dev-secret-key-change-in-production
DJANGO_DEBUG=true
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
POSTGRES_DB=agenda_service
POSTGRES_USER=agenda_service
POSTGRES_PASSWORD=agenda_service
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
```

Copie para `.env` antes de rodar qualquer comando (`cp .env.example .env`).

- [ ] **Step 4: Criar `agendaservice/__init__.py` (vazio) e `agendaservice/settings.py`**

```python
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-secret-key-change-in-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "cerc_shared",
    "agenda_ur",
    "politica_consulta",
]

MIDDLEWARE = []

ROOT_URLCONF = "agendaservice.urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "agenda_service"),
        "USER": os.environ.get("POSTGRES_USER", "agenda_service"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "agenda_service"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        # Schema `cerc` e' compartilhado e sempre visivel; `agenda` e' o schema
        # padrao (pool) usado fora do contexto de requisicao HTTP (testes,
        # comandos de manutencao). O roteamento por tenant real (schema
        # dedicado por financiador) e' implementado no Plano 2 via middleware.
        "OPTIONS": {"options": "-c search_path=agenda,cerc,public"},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
TIME_ZONE = "UTC"
```

- [ ] **Step 5: Criar `agendaservice/urls.py`**

```python
from django.urls import path

urlpatterns: list = []
```

- [ ] **Step 6: Criar `manage.py`**

```python
#!/usr/bin/env python
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agendaservice.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
```

- [ ] **Step 7: Criar `pytest.ini`**

```ini
[pytest]
DJANGO_SETTINGS_MODULE = agendaservice.settings
python_files = test_*.py
```

- [ ] **Step 8: Criar `.gitignore`**

```text
__pycache__/
*.pyc
.env
.venv/
venv/
*.egg-info/
```

- [ ] **Step 9: Instalar dependências e subir o Postgres local**

Run: `pip install -r requirements.txt && cp .env.example .env && docker compose up -d`
Expected: container `postgres` sobe e fica saudável (`docker compose ps` mostra `running`/`healthy`).

- [ ] **Step 10: Commit**

```bash
git add requirements.txt manage.py agendaservice/ docker-compose.yml .env.example pytest.ini .gitignore
git commit -m "chore: scaffolding do projeto Django e Postgres local"
```

---

### Task 2: App `cerc_shared` — schema `cerc` (domínios, funções, tabelas de infraestrutura)

**Files:**
- Create: `cerc_shared/__init__.py`
- Create: `cerc_shared/apps.py`
- Create: `cerc_shared/models.py`
- Create: `cerc_shared/migrations/__init__.py`
- Create: `cerc_shared/migrations/0001_create_cerc_schema.py`
- Test: `cerc_shared/tests/__init__.py`
- Test: `cerc_shared/tests/test_cerc_schema.py`

**Interfaces:**
- Produces: schema `cerc` com domínios `cerc.documento`, `cerc.valor_monetario`, `cerc.ulid`, `cerc.protocolo`; funções `cerc.touch()` e `cerc.precedencia_origem(text) -> int`; tabelas `cerc.dominio_arranjo`, `cerc.participante_slc`, `cerc.cerc_requisicao`, `cerc.webhook_inbox`. Modelos Django: `cerc_shared.models.DominioArranjo`, `ParticipanteSlc`, `CercRequisicao`, `WebhookInbox`.
- Consumido por: Task 3 (tabelas `agenda.*` usam os domínios `cerc.*`), Task 5 (repositório de upsert usa `cerc.precedencia_origem`).

> **Nota de escopo:** o schema `cerc` é "compartilhado, dono: plataforma" (SPEC 04 §2). Como hoje só existem 3 serviços (`optin`, `agenda`, `contrato`) e nenhum repositório de plataforma separado, este plano faz o bootstrap do schema aqui para que o `agenda-service` seja testável de forma independente. Isso é uma decisão pragmática, não definitiva — revisar quando o `optin-service` assumir formalmente a posse deste schema (ver design doc §14).

- [ ] **Step 1: Escrever o teste que falha (schema/domínio/função ainda não existem)**

`cerc_shared/tests/__init__.py` (vazio).

`cerc_shared/tests/test_cerc_schema.py`:

```python
import pytest
from django.db import connection


@pytest.mark.django_db
def test_schema_cerc_existe():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name = %s",
            ["cerc"],
        )
        assert cursor.fetchone() is not None


@pytest.mark.django_db
def test_funcao_precedencia_origem_reflete_webhook_sincrono_arquivo():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT cerc.precedencia_origem('WEBHOOK'), "
            "cerc.precedencia_origem('SINCRONO'), "
            "cerc.precedencia_origem('ARQUIVO')"
        )
        webhook, sincrono, arquivo = cursor.fetchone()
        assert webhook > sincrono > arquivo


@pytest.mark.django_db
def test_dominio_cerc_documento_rejeita_tamanho_invalido():
    with connection.cursor() as cursor:
        with pytest.raises(Exception):
            cursor.execute("SELECT '123'::cerc.documento")
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `pytest cerc_shared/tests/test_cerc_schema.py -v`
Expected: FAIL — `relation "cerc.dominio_arranjo" does not exist` / `schema "cerc" does not exist` (o app `cerc_shared` ainda não tem migration).

- [ ] **Step 3: Criar `cerc_shared/__init__.py` (vazio) e `cerc_shared/apps.py`**

```python
from django.apps import AppConfig


class CercSharedConfig(AppConfig):
    name = "cerc_shared"
    default_auto_field = "django.db.models.BigAutoField"
```

- [ ] **Step 4: Criar `cerc_shared/migrations/__init__.py` (vazio) e a migration `0001_create_cerc_schema.py`**

```python
from django.db import migrations

CREATE_CERC_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS cerc;

CREATE DOMAIN cerc.documento AS TEXT
  CHECK (VALUE ~ '^[0-9]{8}$|^[0-9]{11}$|^[0-9]{14}$');

CREATE DOMAIN cerc.valor_monetario AS NUMERIC(18,2)
  CHECK (VALUE >= 0);

CREATE DOMAIN cerc.ulid AS TEXT
  CHECK (char_length(VALUE) = 26);

CREATE DOMAIN cerc.protocolo AS TEXT
  CHECK (VALUE ~ '^[0-9a-fA-F-]{36}$');

CREATE FUNCTION cerc.touch() RETURNS TRIGGER AS $$
BEGIN
  NEW.atualizado_em := now();
  NEW.versao := OLD.versao + 1;
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE FUNCTION cerc.precedencia_origem(o TEXT) RETURNS INT
  IMMUTABLE LANGUAGE sql AS
$$ SELECT CASE o WHEN 'WEBHOOK' THEN 3 WHEN 'SINCRONO' THEN 2 ELSE 1 END $$;

CREATE TABLE cerc.dominio_arranjo (
  codigo          TEXT PRIMARY KEY,
  descricao       TEXT,
  ativo           BOOLEAN NOT NULL DEFAULT true,
  sincronizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE cerc.participante_slc (
  ispb            TEXT PRIMARY KEY CHECK (ispb ~ '^[0-9]{8}$'),
  compe           TEXT CHECK (compe ~ '^[0-9]{3}$'),
  nome            TEXT NOT NULL,
  ativo           BOOLEAN NOT NULL DEFAULT true,
  sincronizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE cerc.cerc_requisicao (
  id            cerc.ulid    NOT NULL,
  recurso       TEXT         NOT NULL,
  operacao      TEXT,
  correlacao_id TEXT         NOT NULL,
  ambiente      TEXT         NOT NULL,
  http_status   INT,
  tentativa     INT          NOT NULL DEFAULT 1,
  duracao_ms    INT,
  request_body  JSONB        NOT NULL,
  response_body JSONB,
  criado_em     TIMESTAMPTZ  NOT NULL DEFAULT now(),
  PRIMARY KEY (id, criado_em)
) PARTITION BY RANGE (criado_em);

CREATE TABLE cerc.cerc_requisicao_default PARTITION OF cerc.cerc_requisicao DEFAULT;

CREATE INDEX ON cerc.cerc_requisicao (correlacao_id);
CREATE INDEX ON cerc.cerc_requisicao (recurso, criado_em DESC);

CREATE TABLE cerc.webhook_inbox (
  id               cerc.ulid   NOT NULL,
  tipo_evento      TEXT        NOT NULL,
  data_hora_evento TIMESTAMPTZ NOT NULL,
  hash_dedupe      TEXT        NOT NULL,
  payload          JSONB       NOT NULL,
  recebido_em      TIMESTAMPTZ NOT NULL DEFAULT now(),
  processado_em    TIMESTAMPTZ,
  tentativas       INT         NOT NULL DEFAULT 0,
  erro             TEXT,
  PRIMARY KEY (id, recebido_em),
  UNIQUE (hash_dedupe, recebido_em)
) PARTITION BY RANGE (recebido_em);

CREATE TABLE cerc.webhook_inbox_default PARTITION OF cerc.webhook_inbox DEFAULT;

CREATE INDEX ON cerc.webhook_inbox (recebido_em) WHERE processado_em IS NULL;
"""

REVERSE_CERC_SCHEMA_SQL = """
DROP TABLE IF EXISTS cerc.webhook_inbox;
DROP TABLE IF EXISTS cerc.cerc_requisicao;
DROP TABLE IF EXISTS cerc.participante_slc;
DROP TABLE IF EXISTS cerc.dominio_arranjo;
DROP FUNCTION IF EXISTS cerc.precedencia_origem(TEXT);
DROP FUNCTION IF EXISTS cerc.touch();
DROP DOMAIN IF EXISTS cerc.protocolo;
DROP DOMAIN IF EXISTS cerc.ulid;
DROP DOMAIN IF EXISTS cerc.valor_monetario;
DROP DOMAIN IF EXISTS cerc.documento;
DROP SCHEMA IF EXISTS cerc CASCADE;
"""


class Migration(migrations.Migration):
    initial = True
    dependencies: list = []
    operations = [
        migrations.RunSQL(sql=CREATE_CERC_SCHEMA_SQL, reverse_sql=REVERSE_CERC_SCHEMA_SQL),
    ]
```

- [ ] **Step 5: Criar `cerc_shared/models.py`**

```python
from django.db import models


class DominioArranjo(models.Model):
    codigo = models.TextField(primary_key=True)
    descricao = models.TextField(null=True)
    ativo = models.BooleanField(default=True)
    sincronizado_em = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "dominio_arranjo"


class ParticipanteSlc(models.Model):
    ispb = models.TextField(primary_key=True)
    compe = models.TextField(null=True)
    nome = models.TextField()
    ativo = models.BooleanField(default=True)
    sincronizado_em = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "participante_slc"


class CercRequisicao(models.Model):
    pk = models.CompositePrimaryKey("id", "criado_em")
    id = models.TextField()
    recurso = models.TextField()
    operacao = models.TextField(null=True)
    correlacao_id = models.TextField()
    ambiente = models.TextField()
    http_status = models.IntegerField(null=True)
    tentativa = models.IntegerField(default=1)
    duracao_ms = models.IntegerField(null=True)
    request_body = models.JSONField()
    response_body = models.JSONField(null=True)
    criado_em = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "cerc_requisicao"


class WebhookInbox(models.Model):
    pk = models.CompositePrimaryKey("id", "recebido_em")
    id = models.TextField()
    tipo_evento = models.TextField()
    data_hora_evento = models.DateTimeField()
    hash_dedupe = models.TextField()
    payload = models.JSONField()
    recebido_em = models.DateTimeField()
    processado_em = models.DateTimeField(null=True)
    tentativas = models.IntegerField(default=0)
    erro = models.TextField(null=True)

    class Meta:
        managed = False
        db_table = "webhook_inbox"
```

- [ ] **Step 6: Rodar as migrations e os testes de novo, confirmar que passam**

Run: `python manage.py migrate && pytest cerc_shared/tests/test_cerc_schema.py -v`
Expected: PASS nos 3 testes.

- [ ] **Step 7: Commit**

```bash
git add cerc_shared/
git commit -m "feat: bootstrap do schema cerc (dominios, funcoes, tabelas de infraestrutura)"
```

---

### Task 3: App `agenda_ur` — schema `agenda` (tabelas núcleo da SPEC 04 §5.4)

**Files:**
- Create: `agenda_ur/__init__.py`
- Create: `agenda_ur/apps.py`
- Create: `agenda_ur/models.py`
- Create: `agenda_ur/migrations/__init__.py`
- Create: `agenda_ur/migrations/0001_create_agenda_schema.py`
- Test: `agenda_ur/tests/__init__.py`
- Test: `agenda_ur/tests/test_agenda_schema.py`

**Interfaces:**
- Consumes: domínios/funções de `cerc_shared` (Task 2) — a migration depende de `cerc_shared.0001_create_cerc_schema`.
- Produces: schema `agenda` com tabelas `consulta_agenda`, `agenda_ur`, `agenda_ur_pagamento`, `consulta_agenda_ur`, `agenda_ur_orfa`, `agenda_ur_rejeitada`, `arquivo_agenda_processado`, `indicador_consistencia_agenda`. Modelos Django: `agenda_ur.models.ConsultaAgenda`, `AgendaUr`, `AgendaUrPagamento`, `ConsultaAgendaUr`, `AgendaUrOrfa`, `AgendaUrRejeitada`, `ArquivoAgendaProcessado`, `IndicadorConsistenciaAgenda`.
- Consumido por: Task 4 (`agenda.politica_consulta` referencia o mesmo schema `agenda`), Task 5 (repositório de upsert grava em `AgendaUr`/`AgendaUrPagamento`).

- [ ] **Step 1: Escrever o teste que falha**

`agenda_ur/tests/__init__.py` (vazio).

`agenda_ur/tests/test_agenda_schema.py`:

```python
from datetime import date, datetime, timezone

import pytest
from django.db import connection

from agenda_ur.models import AgendaUr


@pytest.mark.django_db
def test_schema_agenda_existe():
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name = %s",
            ["agenda"],
        )
        assert cursor.fetchone() is not None


@pytest.mark.django_db
def test_insercao_direta_cai_na_particao_default_de_agenda_ur():
    AgendaUr.objects.create(
        entidade_registradora="23399607000191",
        cnpj_credenciadora="36216798000150",
        documento_ufr="22751826000125",
        documento_titular="22751826000125",
        codigo_arranjo="VCC",
        data_liquidacao=date(2026, 9, 4),
        constituicao="1",
        valor_constituido_total="1000.00",
        valor_constituido_antecipacao_pre="0.00",
        valor_bloqueado="0.00",
        valor_livre="1000.00",
        valor_total_ur="1000.00",
        data_hora_ultima_atualizacao=datetime(2026, 8, 17, 4, 58, 36, tzinfo=timezone.utc),
        origem="ARQUIVO",
        origem_arquivo="AP005",
        atualizado_em=datetime.now(tz=timezone.utc),
    )

    row = AgendaUr.objects.get(
        data_liquidacao=date(2026, 9, 4),
        entidade_registradora="23399607000191",
        cnpj_credenciadora="36216798000150",
        documento_ufr="22751826000125",
        documento_titular="22751826000125",
        codigo_arranjo="VCC",
    )
    assert str(row.valor_livre) == "1000.00"
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `pytest agenda_ur/tests/test_agenda_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agenda_ur.models'` (o app ainda não existe).

- [ ] **Step 3: Criar `agenda_ur/__init__.py` (vazio) e `agenda_ur/apps.py`**

```python
from django.apps import AppConfig


class AgendaUrConfig(AppConfig):
    name = "agenda_ur"
    default_auto_field = "django.db.models.BigAutoField"
```

- [ ] **Step 4: Criar `agenda_ur/migrations/__init__.py` (vazio) e a migration `0001_create_agenda_schema.py`**

```python
from django.db import migrations

CREATE_AGENDA_SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS agenda;

CREATE TABLE agenda.consulta_agenda (
  id                     cerc.ulid PRIMARY KEY,
  modo                   TEXT NOT NULL CHECK (modo IN ('ONLINE','BATCH')),
  status                 TEXT NOT NULL CHECK (status IN
    ('PARCIAL','COMPLETA','COMPLETA_COM_TIMEOUT','ERRO')),
  filtro_ufr             cerc.documento NOT NULL,
  filtro_titular         cerc.documento,
  filtro_credenciadoras  TEXT[] NOT NULL,
  filtro_arranjos        TEXT[] NOT NULL,
  filtro_data_inicio     DATE NOT NULL,
  filtro_data_fim        DATE NOT NULL,
  tipo_avaliacao         TEXT,
  carteira               TEXT,
  base_autorizativa_tipo TEXT NOT NULL CHECK (base_autorizativa_tipo IN ('OPTIN','CONTRATO')),
  base_autorizativa_id   cerc.ulid NOT NULL,
  motivo                 TEXT NOT NULL,
  ator                   TEXT NOT NULL,
  origem_ip              INET,
  qtd_urs_sincrono       INT NOT NULL DEFAULT 0,
  qtd_urs_webhook        INT NOT NULL DEFAULT 0,
  iniciada_em            TIMESTAMPTZ NOT NULL DEFAULT now(),
  ultima_ur_em           TIMESTAMPTZ,
  encerrada_em           TIMESTAMPTZ,
  CONSTRAINT filtro_datas_coerentes CHECK (filtro_data_fim >= filtro_data_inicio)
);

CREATE INDEX ON agenda.consulta_agenda (filtro_ufr, iniciada_em DESC);
CREATE INDEX ON agenda.consulta_agenda (ultima_ur_em) WHERE status = 'PARCIAL';
CREATE INDEX ON agenda.consulta_agenda (filtro_ufr, iniciada_em) WHERE modo = 'ONLINE';

CREATE TABLE agenda.agenda_ur (
  entidade_registradora cerc.documento NOT NULL,
  cnpj_credenciadora    cerc.documento NOT NULL,
  documento_ufr         cerc.documento NOT NULL,
  documento_titular     cerc.documento NOT NULL,
  codigo_arranjo        TEXT NOT NULL,
  data_liquidacao       DATE NOT NULL,
  constituicao          TEXT NOT NULL CHECK (constituicao IN ('1','2')),
  valor_constituido_total           cerc.valor_monetario NOT NULL,
  valor_constituido_antecipacao_pre cerc.valor_monetario NOT NULL DEFAULT 0,
  valor_bloqueado       cerc.valor_monetario NOT NULL DEFAULT 0,
  valor_livre           cerc.valor_monetario NOT NULL DEFAULT 0,
  valor_total_ur        cerc.valor_monetario NOT NULL,
  carteira              TEXT,
  data_hora_ultima_atualizacao TIMESTAMPTZ NOT NULL,
  origem                TEXT NOT NULL CHECK (origem IN ('SINCRONO','WEBHOOK','ARQUIVO')),
  origem_arquivo        TEXT CHECK (origem_arquivo IN ('AP005','AP005A','AP005B')),
  atualizado_em         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (data_liquidacao, entidade_registradora, cnpj_credenciadora,
               documento_ufr, documento_titular, codigo_arranjo)
) PARTITION BY RANGE (data_liquidacao);

CREATE TABLE agenda.agenda_ur_default PARTITION OF agenda.agenda_ur DEFAULT;

CREATE INDEX ON agenda.agenda_ur (documento_ufr, data_liquidacao)
  INCLUDE (valor_livre, valor_constituido_total, valor_bloqueado, constituicao);
CREATE INDEX ON agenda.agenda_ur (documento_ufr, data_liquidacao)
  WHERE constituicao = '1';
CREATE INDEX ON agenda.agenda_ur (data_hora_ultima_atualizacao);

CREATE TABLE agenda.agenda_ur_pagamento (
  data_liquidacao        DATE NOT NULL,
  entidade_registradora  cerc.documento NOT NULL,
  cnpj_credenciadora     cerc.documento NOT NULL,
  documento_ufr          cerc.documento NOT NULL,
  documento_titular      cerc.documento NOT NULL,
  codigo_arranjo         TEXT NOT NULL,
  tipo_informacao_pagamento TEXT NOT NULL
    CHECK (tipo_informacao_pagamento IN ('1','2','3','4','5','6','7','8')),
  indicador_efeitos_contrato TEXT NOT NULL DEFAULT '',
  identificador_cerc_contrato TEXT,
  regras_divisao         TEXT CHECK (regras_divisao IN ('1','2')),
  valor_onerado          NUMERIC(18,2),
  valor_constituido_efeito cerc.valor_monetario,
  valor_a_pagar          cerc.valor_monetario,
  beneficiario           cerc.documento,
  data_liquidacao_efetiva DATE,
  valor_liquidacao_efetiva cerc.valor_monetario,
  motivo_nao_pagamento   TEXT CHECK (motivo_nao_pagamento IN ('001','002','999')),
  domicilio              JSONB NOT NULL,
  atualizado_em          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (data_liquidacao, entidade_registradora, cnpj_credenciadora,
               documento_ufr, documento_titular, codigo_arranjo,
               tipo_informacao_pagamento, indicador_efeitos_contrato)
) PARTITION BY RANGE (data_liquidacao);

CREATE TABLE agenda.agenda_ur_pagamento_default PARTITION OF agenda.agenda_ur_pagamento DEFAULT;

CREATE INDEX ON agenda.agenda_ur_pagamento (identificador_cerc_contrato)
  WHERE identificador_cerc_contrato IS NOT NULL;
CREATE INDEX ON agenda.agenda_ur_pagamento (documento_ufr, data_liquidacao)
  WHERE tipo_informacao_pagamento IN ('1','2','3','4','8');

CREATE TABLE agenda.consulta_agenda_ur (
  consulta_id           cerc.ulid NOT NULL,
  entidade_registradora cerc.documento NOT NULL,
  cnpj_credenciadora    cerc.documento NOT NULL,
  documento_ufr         cerc.documento NOT NULL,
  documento_titular     cerc.documento NOT NULL,
  codigo_arranjo        TEXT NOT NULL,
  data_liquidacao       DATE NOT NULL,
  origem                TEXT NOT NULL,
  recebida_em           TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (recebida_em, consulta_id, entidade_registradora, cnpj_credenciadora,
               documento_ufr, documento_titular, codigo_arranjo, data_liquidacao)
) PARTITION BY RANGE (recebida_em);

CREATE TABLE agenda.consulta_agenda_ur_default PARTITION OF agenda.consulta_agenda_ur DEFAULT;

CREATE INDEX ON agenda.consulta_agenda_ur (consulta_id);

CREATE TABLE agenda.agenda_ur_orfa (
  id          BIGSERIAL PRIMARY KEY,
  payload     JSONB NOT NULL,
  recebida_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolvida_em TIMESTAMPTZ
);

CREATE TABLE agenda.agenda_ur_rejeitada (
  id         BIGSERIAL PRIMARY KEY,
  origem     TEXT NOT NULL,
  arquivo    TEXT,
  linha      INT,
  conteudo   TEXT,
  motivo     TEXT NOT NULL,
  ocorrida_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agenda.arquivo_agenda_processado (
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

CREATE TABLE agenda.indicador_consistencia_agenda (
  consulta_id cerc.ulid NOT NULL REFERENCES agenda.consulta_agenda(id) ON DELETE CASCADE,
  indicador   TEXT NOT NULL,
  resultado   TEXT NOT NULL,
  parametros  JSONB,
  criticidade TEXT NOT NULL CHECK (criticidade IN ('0','1','2','3')),
  PRIMARY KEY (consulta_id, indicador)
);
"""

REVERSE_AGENDA_SCHEMA_SQL = """
DROP TABLE IF EXISTS agenda.indicador_consistencia_agenda;
DROP TABLE IF EXISTS agenda.arquivo_agenda_processado;
DROP TABLE IF EXISTS agenda.agenda_ur_rejeitada;
DROP TABLE IF EXISTS agenda.agenda_ur_orfa;
DROP TABLE IF EXISTS agenda.consulta_agenda_ur;
DROP TABLE IF EXISTS agenda.agenda_ur_pagamento;
DROP TABLE IF EXISTS agenda.agenda_ur;
DROP TABLE IF EXISTS agenda.consulta_agenda;
DROP SCHEMA IF EXISTS agenda CASCADE;
"""


class Migration(migrations.Migration):
    initial = True
    dependencies = [("cerc_shared", "0001_create_cerc_schema")]
    operations = [
        migrations.RunSQL(sql=CREATE_AGENDA_SCHEMA_SQL, reverse_sql=REVERSE_AGENDA_SCHEMA_SQL),
    ]
```

- [ ] **Step 5: Criar `agenda_ur/models.py`**

```python
from django.contrib.postgres.fields import ArrayField
from django.db import models


class ConsultaAgenda(models.Model):
    id = models.TextField(primary_key=True)
    modo = models.TextField()
    status = models.TextField()
    filtro_ufr = models.TextField()
    filtro_titular = models.TextField(null=True)
    filtro_credenciadoras = ArrayField(models.TextField())
    filtro_arranjos = ArrayField(models.TextField())
    filtro_data_inicio = models.DateField()
    filtro_data_fim = models.DateField()
    tipo_avaliacao = models.TextField(null=True)
    carteira = models.TextField(null=True)
    base_autorizativa_tipo = models.TextField()
    base_autorizativa_id = models.TextField()
    motivo = models.TextField()
    ator = models.TextField()
    origem_ip = models.GenericIPAddressField(null=True)
    qtd_urs_sincrono = models.IntegerField(default=0)
    qtd_urs_webhook = models.IntegerField(default=0)
    iniciada_em = models.DateTimeField()
    ultima_ur_em = models.DateTimeField(null=True)
    encerrada_em = models.DateTimeField(null=True)

    class Meta:
        managed = False
        db_table = "consulta_agenda"


class AgendaUr(models.Model):
    pk = models.CompositePrimaryKey(
        "data_liquidacao", "entidade_registradora", "cnpj_credenciadora",
        "documento_ufr", "documento_titular", "codigo_arranjo",
    )
    entidade_registradora = models.TextField()
    cnpj_credenciadora = models.TextField()
    documento_ufr = models.TextField()
    documento_titular = models.TextField()
    codigo_arranjo = models.TextField()
    data_liquidacao = models.DateField()
    constituicao = models.TextField()
    valor_constituido_total = models.DecimalField(max_digits=18, decimal_places=2)
    valor_constituido_antecipacao_pre = models.DecimalField(max_digits=18, decimal_places=2)
    valor_bloqueado = models.DecimalField(max_digits=18, decimal_places=2)
    valor_livre = models.DecimalField(max_digits=18, decimal_places=2)
    valor_total_ur = models.DecimalField(max_digits=18, decimal_places=2)
    carteira = models.TextField(null=True)
    data_hora_ultima_atualizacao = models.DateTimeField()
    origem = models.TextField()
    origem_arquivo = models.TextField(null=True)
    atualizado_em = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "agenda_ur"


class AgendaUrPagamento(models.Model):
    pk = models.CompositePrimaryKey(
        "data_liquidacao", "entidade_registradora", "cnpj_credenciadora",
        "documento_ufr", "documento_titular", "codigo_arranjo",
        "tipo_informacao_pagamento", "indicador_efeitos_contrato",
    )
    data_liquidacao = models.DateField()
    entidade_registradora = models.TextField()
    cnpj_credenciadora = models.TextField()
    documento_ufr = models.TextField()
    documento_titular = models.TextField()
    codigo_arranjo = models.TextField()
    tipo_informacao_pagamento = models.TextField()
    indicador_efeitos_contrato = models.TextField(default="")
    identificador_cerc_contrato = models.TextField(null=True)
    regras_divisao = models.TextField(null=True)
    valor_onerado = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    valor_constituido_efeito = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    valor_a_pagar = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    beneficiario = models.TextField(null=True)
    data_liquidacao_efetiva = models.DateField(null=True)
    valor_liquidacao_efetiva = models.DecimalField(max_digits=18, decimal_places=2, null=True)
    motivo_nao_pagamento = models.TextField(null=True)
    domicilio = models.JSONField()
    atualizado_em = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "agenda_ur_pagamento"


class ConsultaAgendaUr(models.Model):
    pk = models.CompositePrimaryKey(
        "recebida_em", "consulta_id", "entidade_registradora", "cnpj_credenciadora",
        "documento_ufr", "documento_titular", "codigo_arranjo", "data_liquidacao",
    )
    consulta_id = models.TextField()
    entidade_registradora = models.TextField()
    cnpj_credenciadora = models.TextField()
    documento_ufr = models.TextField()
    documento_titular = models.TextField()
    codigo_arranjo = models.TextField()
    data_liquidacao = models.DateField()
    origem = models.TextField()
    recebida_em = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "consulta_agenda_ur"


class AgendaUrOrfa(models.Model):
    id = models.BigAutoField(primary_key=True)
    payload = models.JSONField()
    recebida_em = models.DateTimeField()
    resolvida_em = models.DateTimeField(null=True)

    class Meta:
        managed = False
        db_table = "agenda_ur_orfa"


class AgendaUrRejeitada(models.Model):
    id = models.BigAutoField(primary_key=True)
    origem = models.TextField()
    arquivo = models.TextField(null=True)
    linha = models.IntegerField(null=True)
    conteudo = models.TextField(null=True)
    motivo = models.TextField()
    ocorrida_em = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "agenda_ur_rejeitada"


class ArquivoAgendaProcessado(models.Model):
    pk = models.CompositePrimaryKey("tipo_leiaute", "ident_ic", "data_req", "seq")
    tipo_leiaute = models.TextField()
    ident_ic = models.TextField()
    data_req = models.DateField()
    seq = models.IntegerField()
    linhas_lidas = models.BigIntegerField(null=True)
    linhas_ok = models.BigIntegerField(null=True)
    linhas_rejeitadas = models.BigIntegerField(null=True)
    iniciado_em = models.DateTimeField(null=True)
    concluido_em = models.DateTimeField(null=True)

    class Meta:
        managed = False
        db_table = "arquivo_agenda_processado"


class IndicadorConsistenciaAgenda(models.Model):
    pk = models.CompositePrimaryKey("consulta_id", "indicador")
    consulta_id = models.TextField()
    indicador = models.TextField()
    resultado = models.TextField()
    parametros = models.JSONField(null=True)
    criticidade = models.TextField()

    class Meta:
        managed = False
        db_table = "indicador_consistencia_agenda"
```

- [ ] **Step 6: Rodar migrations e os testes de novo, confirmar que passam**

Run: `python manage.py migrate && pytest agenda_ur/tests/test_agenda_schema.py -v`
Expected: PASS nos 2 testes.

- [ ] **Step 7: Commit**

```bash
git add agenda_ur/
git commit -m "feat: cria schema agenda e tabelas nucleo da SPEC 04 secao 5.4"
```

---

### Task 4: App `politica_consulta` — configuração self-service de modo por finalidade

**Files:**
- Create: `politica_consulta/__init__.py`
- Create: `politica_consulta/apps.py`
- Create: `politica_consulta/models.py`
- Create: `politica_consulta/migrations/__init__.py`
- Create: `politica_consulta/migrations/0001_create_politica_consulta.py`
- Test: `politica_consulta/tests/__init__.py`
- Test: `politica_consulta/tests/test_politica_consulta.py`

**Interfaces:**
- Consumes: schema `agenda` (Task 3) e função `cerc.touch()` (Task 2) — migration depende de `agenda_ur.0001_create_agenda_schema`.
- Produces: tabela `agenda.politica_consulta`; modelo `politica_consulta.models.PoliticaConsulta`. Usado pela futura validação `A10` (Plano de integração CERC, fora deste plano).

- [ ] **Step 1: Escrever o teste que falha**

`politica_consulta/tests/__init__.py` (vazio).

`politica_consulta/tests/test_politica_consulta.py`:

```python
import pytest
from django.db import IntegrityError, connection, transaction

from politica_consulta.models import PoliticaConsulta


@pytest.mark.django_db
def test_cria_politica_com_modos_permitidos():
    PoliticaConsulta.objects.create(
        id="01J8ZKPOLITICA0000000001A",
        cnpj_financiador="12345678000199",
        motivo="ANALISE_CREDITO",
        modos_permitidos=["ONLINE"],
    )

    politica = PoliticaConsulta.objects.get(
        cnpj_financiador="12345678000199", motivo="ANALISE_CREDITO"
    )
    assert politica.modos_permitidos == ["ONLINE"]
    assert politica.ativo is True


@pytest.mark.django_db
def test_unicidade_por_financiador_e_motivo():
    PoliticaConsulta.objects.create(
        id="01J8ZKPOLITICA0000000002A",
        cnpj_financiador="12345678000199",
        motivo="MONITORAMENTO",
        modos_permitidos=["BATCH"],
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            PoliticaConsulta.objects.create(
                id="01J8ZKPOLITICA0000000003A",
                cnpj_financiador="12345678000199",
                motivo="MONITORAMENTO",
                modos_permitidos=["ONLINE"],
            )


@pytest.mark.django_db
def test_modos_permitidos_rejeita_valor_fora_do_dominio():
    with connection.cursor() as cursor:
        with pytest.raises(Exception):
            cursor.execute(
                "INSERT INTO agenda.politica_consulta "
                "(id, cnpj_financiador, motivo, modos_permitidos) "
                "VALUES (%s, %s, %s, %s)",
                ["01J8ZKPOLITICA0000000004A", "12345678000199", "X", ["INVALIDO"]],
            )
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `pytest politica_consulta/tests/test_politica_consulta.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'politica_consulta.models'`.

- [ ] **Step 3: Criar `politica_consulta/__init__.py` (vazio) e `politica_consulta/apps.py`**

```python
from django.apps import AppConfig


class PoliticaConsultaConfig(AppConfig):
    name = "politica_consulta"
    default_auto_field = "django.db.models.BigAutoField"
```

- [ ] **Step 4: Criar `politica_consulta/migrations/__init__.py` (vazio) e a migration `0001_create_politica_consulta.py`**

```python
from django.db import migrations

CREATE_POLITICA_CONSULTA_SQL = """
CREATE TABLE agenda.politica_consulta (
  id                 cerc.ulid PRIMARY KEY,
  cnpj_financiador   cerc.documento NOT NULL,
  motivo             TEXT NOT NULL,
  modos_permitidos   TEXT[] NOT NULL CHECK (modos_permitidos <@ ARRAY['BATCH','ONLINE']),
  ativo              BOOLEAN NOT NULL DEFAULT true,
  criado_em          TIMESTAMPTZ NOT NULL DEFAULT now(),
  atualizado_em      TIMESTAMPTZ NOT NULL DEFAULT now(),
  versao             INT NOT NULL DEFAULT 1,
  CONSTRAINT politica_unica UNIQUE (cnpj_financiador, motivo)
);

CREATE TRIGGER politica_consulta_touch
  BEFORE UPDATE ON agenda.politica_consulta
  FOR EACH ROW EXECUTE FUNCTION cerc.touch();
"""

REVERSE_POLITICA_CONSULTA_SQL = """
DROP TABLE IF EXISTS agenda.politica_consulta CASCADE;
"""


class Migration(migrations.Migration):
    initial = True
    dependencies = [("agenda_ur", "0001_create_agenda_schema")]
    operations = [
        migrations.RunSQL(sql=CREATE_POLITICA_CONSULTA_SQL, reverse_sql=REVERSE_POLITICA_CONSULTA_SQL),
    ]
```

- [ ] **Step 5: Criar `politica_consulta/models.py`**

```python
from django.contrib.postgres.fields import ArrayField
from django.db import models


class PoliticaConsulta(models.Model):
    id = models.TextField(primary_key=True)
    cnpj_financiador = models.TextField()
    motivo = models.TextField()
    modos_permitidos = ArrayField(models.TextField())
    ativo = models.BooleanField(default=True)
    criado_em = models.DateTimeField(auto_now_add=True)
    # auto_now (nao auto_now_add): em updates via ORM, deve refletir o mesmo
    # comportamento do trigger cerc.touch() (Task 4) - atualiza a cada save.
    atualizado_em = models.DateTimeField(auto_now=True)
    versao = models.IntegerField(default=1)

    class Meta:
        managed = False
        db_table = "politica_consulta"
```

- [ ] **Step 6: Rodar migrations e os testes de novo, confirmar que passam**

Run: `python manage.py migrate && pytest politica_consulta/tests/test_politica_consulta.py -v`
Expected: PASS nos 3 testes.

- [ ] **Step 7: Commit**

```bash
git add politica_consulta/
git commit -m "feat: tabela e modelo de politica de consulta self-service"
```

---

### Task 5: Repositório de upsert de `agenda_ur` (regra de precedência de frescor)

**Files:**
- Create: `agenda_ur/repository.py`
- Test: `agenda_ur/tests/test_repository.py`

**Interfaces:**
- Consumes: `cerc.precedencia_origem` (Task 2), tabela `agenda.agenda_ur` (Task 3).
- Produces: `agenda_ur.repository.upsert_agenda_ur(ur: dict) -> None` — usado pelos futuros planos de integração CERC (Plano 3), webhook (Plano 4) e ingestão de arquivo (Plano 5) como o único caminho de escrita em `agenda.agenda_ur`.

- [ ] **Step 1: Escrever os testes que falham**

`agenda_ur/tests/test_repository.py`:

```python
from datetime import date, datetime, timedelta, timezone

import pytest

from agenda_ur.models import AgendaUr
from agenda_ur.repository import upsert_agenda_ur


def _ur(**overrides):
    base = {
        "entidade_registradora": "23399607000191",
        "cnpj_credenciadora": "36216798000150",
        "documento_ufr": "22751826000125",
        "documento_titular": "22751826000125",
        "codigo_arranjo": "VCC",
        "data_liquidacao": date(2026, 9, 4),
        "constituicao": "1",
        "valor_constituido_total": "1000.00",
        "valor_constituido_antecipacao_pre": "0.00",
        "valor_bloqueado": "0.00",
        "valor_livre": "1000.00",
        "valor_total_ur": "1000.00",
        "carteira": None,
        "data_hora_ultima_atualizacao": datetime(2026, 8, 17, 4, 58, 36, tzinfo=timezone.utc),
        "origem": "ARQUIVO",
        "origem_arquivo": "AP005",
    }
    base.update(overrides)
    return base


def _fetch():
    return AgendaUr.objects.get(
        data_liquidacao=date(2026, 9, 4),
        entidade_registradora="23399607000191",
        cnpj_credenciadora="36216798000150",
        documento_ufr="22751826000125",
        documento_titular="22751826000125",
        codigo_arranjo="VCC",
    )


@pytest.mark.django_db
def test_insercao_inicial_grava_a_linha():
    upsert_agenda_ur(_ur())

    row = _fetch()
    assert row.origem == "ARQUIVO"
    assert str(row.valor_livre) == "1000.00"


@pytest.mark.django_db
def test_dado_mais_antigo_nao_sobrescreve():
    t0 = datetime(2026, 8, 17, 4, 58, 36, tzinfo=timezone.utc)
    upsert_agenda_ur(_ur(data_hora_ultima_atualizacao=t0, origem="ARQUIVO", valor_livre="100.00"))

    upsert_agenda_ur(_ur(
        data_hora_ultima_atualizacao=t0 - timedelta(seconds=1),
        origem="SINCRONO",
        valor_livre="999.00",
    ))

    row = _fetch()
    assert row.origem == "ARQUIVO"
    assert str(row.valor_livre) == "100.00"


@pytest.mark.django_db
def test_empate_de_horario_resolve_por_precedencia_de_origem():
    t0 = datetime(2026, 8, 17, 4, 58, 36, tzinfo=timezone.utc)
    upsert_agenda_ur(_ur(data_hora_ultima_atualizacao=t0, origem="ARQUIVO", valor_livre="100.00"))

    upsert_agenda_ur(_ur(data_hora_ultima_atualizacao=t0, origem="SINCRONO", valor_livre="200.00"))
    row = _fetch()
    assert row.origem == "SINCRONO"
    assert str(row.valor_livre) == "200.00"

    upsert_agenda_ur(_ur(data_hora_ultima_atualizacao=t0, origem="ARQUIVO", valor_livre="300.00"))
    row = _fetch()
    assert row.origem == "SINCRONO", "ARQUIVO nao pode sobrescrever SINCRONO no mesmo horario"
    assert str(row.valor_livre) == "200.00"


@pytest.mark.django_db
def test_horario_mais_novo_vence_mesmo_com_origem_de_menor_precedencia():
    t0 = datetime(2026, 8, 17, 4, 58, 36, tzinfo=timezone.utc)
    upsert_agenda_ur(_ur(data_hora_ultima_atualizacao=t0, origem="WEBHOOK", valor_livre="200.00"))

    upsert_agenda_ur(_ur(
        data_hora_ultima_atualizacao=t0 + timedelta(seconds=10),
        origem="ARQUIVO",
        valor_livre="300.00",
    ))

    row = _fetch()
    assert row.origem == "ARQUIVO"
    assert str(row.valor_livre) == "300.00"
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `pytest agenda_ur/tests/test_repository.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agenda_ur.repository'`.

- [ ] **Step 3: Criar `agenda_ur/repository.py`**

```python
from django.db import connection

UPSERT_AGENDA_UR_SQL = """
INSERT INTO agenda.agenda_ur (
    entidade_registradora, cnpj_credenciadora, documento_ufr, documento_titular,
    codigo_arranjo, data_liquidacao, constituicao, valor_constituido_total,
    valor_constituido_antecipacao_pre, valor_bloqueado, valor_livre, valor_total_ur,
    carteira, data_hora_ultima_atualizacao, origem, origem_arquivo, atualizado_em
) VALUES (
    %(entidade_registradora)s, %(cnpj_credenciadora)s, %(documento_ufr)s, %(documento_titular)s,
    %(codigo_arranjo)s, %(data_liquidacao)s, %(constituicao)s, %(valor_constituido_total)s,
    %(valor_constituido_antecipacao_pre)s, %(valor_bloqueado)s, %(valor_livre)s, %(valor_total_ur)s,
    %(carteira)s, %(data_hora_ultima_atualizacao)s, %(origem)s, %(origem_arquivo)s, now()
)
ON CONFLICT (data_liquidacao, entidade_registradora, cnpj_credenciadora,
             documento_ufr, documento_titular, codigo_arranjo)
DO UPDATE SET
    constituicao = EXCLUDED.constituicao,
    valor_constituido_total = EXCLUDED.valor_constituido_total,
    valor_constituido_antecipacao_pre = EXCLUDED.valor_constituido_antecipacao_pre,
    valor_bloqueado = EXCLUDED.valor_bloqueado,
    valor_livre = EXCLUDED.valor_livre,
    valor_total_ur = EXCLUDED.valor_total_ur,
    carteira = EXCLUDED.carteira,
    data_hora_ultima_atualizacao = EXCLUDED.data_hora_ultima_atualizacao,
    origem = EXCLUDED.origem,
    origem_arquivo = EXCLUDED.origem_arquivo,
    atualizado_em = now()
WHERE EXCLUDED.data_hora_ultima_atualizacao > agenda_ur.data_hora_ultima_atualizacao
   OR (EXCLUDED.data_hora_ultima_atualizacao = agenda_ur.data_hora_ultima_atualizacao
       AND cerc.precedencia_origem(EXCLUDED.origem) > cerc.precedencia_origem(agenda_ur.origem))
"""


def upsert_agenda_ur(ur: dict) -> None:
    """Insere ou atualiza uma UR respeitando a regra de precedencia de
    frescor da SPEC 04 secao 5.5: so sobrescreve se o dado novo tiver
    data_hora_ultima_atualizacao mais recente, ou empatado com origem de
    maior precedencia (WEBHOOK > SINCRONO > ARQUIVO). Um arquivo AP005
    atrasado nunca sobrescreve um dado mais recente vindo de webhook.
    """
    with connection.cursor() as cursor:
        cursor.execute(UPSERT_AGENDA_UR_SQL, ur)
```

- [ ] **Step 4: Rodar os testes de novo, confirmar que passam**

Run: `pytest agenda_ur/tests/test_repository.py -v`
Expected: PASS nos 4 testes.

- [ ] **Step 5: Rodar a suíte completa do plano**

Run: `pytest -v`
Expected: todos os testes de `cerc_shared`, `agenda_ur` e `politica_consulta` passam.

- [ ] **Step 6: Commit**

```bash
git add agenda_ur/repository.py agenda_ur/tests/test_repository.py
git commit -m "feat: repositorio de upsert de agenda_ur com precedencia de frescor"
```

---

## O que este plano não cobre (fica para os próximos)

- Roteamento de tenant real (middleware de `search_path`/RLS) — Plano 2.
- Cliente CERC, validações `A01`–`A10` e integração com `optin-service` para token — Plano 3.
- Webhook, correlação e critério de completude — Plano 4.
- Ingestão de arquivo AP005/AP005A/AP005B (incluindo o repositório de carga em massa via `COPY`) — Plano 5.
- Provisionamento de um novo schema dedicado quando um financiador de porte grande é cadastrado — depende do `optin-service`; hoje só o schema `agenda` (pool) existe.
