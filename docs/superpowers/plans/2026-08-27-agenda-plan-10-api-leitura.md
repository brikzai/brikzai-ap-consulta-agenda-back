# agenda-service — Plan 10: API Interna — Leitura/Relatório (`agendas/urs`, `agendas/urs/posicao`, `compliance/relatorio`) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Nota de série:** design doc §16 chama este passo de "Plano 10 — API interna de leitura/relatório (`GET /agendas/urs`, `GET /agendas/urs/posicao`, `GET /compliance/relatorio`) + compliance" — os três endpoints de leitura que o Plano 09 deixou de fora (ver sua Nota de série) porque nenhum dos dois serviços irmãos tem paginação por cursor ou agregação (`SUM`/`GROUP BY`) já construídas para copiar. Brainstorm feito antes deste plano decidiu: estender `shared/cloudsql_client.py` com `.gt()` e `.group_by()` (agregação real no banco, não em Python) em vez de trazer tudo pra memória e somar; adicionar uma coluna `sequencia BIGSERIAL` em `agenda_ur` como chave de cursor (a PK composta de 6 colunas não serve pra isso); reaproveitar `consulta_agenda.id` (já um ULID, ordenável por tempo) como cursor do relatório de compliance sem nenhuma mudança de schema. O risco 20 (N+1 de `GET /agendas/consultas/{id}`, achado na revisão final do Plano 09) fica **de fora** deste plano — precisa de uma capacidade diferente (filtro `IN`, não `GROUP BY`) e é tratado num plano/task futuro dedicado.

**Goal:** Expor `GET /api/v1/agendas/urs` (repositório consolidado de URs, filtros + paginação por cursor), `GET /api/v1/agendas/urs/posicao` (visão agregada de crédito por UFR/janela, fumaça sempre segregada) e `GET /api/v1/compliance/relatorio` (trilha de consultas por período/UFR/ator, paginado) — os três `@jwt_required`, os três só leem o repositório consolidado (nenhum chama a CERC).

**Architecture:** Cinco tasks. Task 1 estende `shared/cloudsql_client.py` com dois primitivos genéricos (`.gt()`, `.group_by()`) usados por todo o resto do plano. Task 2 é a migração de schema que dá a `agenda_ur` uma chave de cursor estável. Tasks 3–5 são um endpoint cada, todos em `apps/agenda/views.py`, reaproveitando `_parse_data_iso`/`_como_datetime`/`jwt_required` já existentes.

**Tech Stack:** Nenhuma dependência nova.

**Spec:** `docs/superpowers/specs/2026-08-24-agenda-service-design.md` §10 (tabela de endpoints + nota de segurança sobre `QueryBuilder` e nomes de coluna vindos de query string), §11 (compliance), §15 riscos 2 e 20. `docs/specs/SPEC-03-consulta-de-agenda-ap005.md` §7.3 (`GET /agendas/urs`), §7.4 (`GET /agendas/urs/posicao`, exemplo de resposta), §8 item 5 (relatório de consultas). `sql/schema/01-agenda-schema.sql` (schema de referência de `agenda_ur`/`agenda_ur_pagamento`/`consulta_agenda`). Série: plano 10 de ~11 (ver Nota de série).

**Depends on:** `2026-08-24-agenda-plan-03-cloudsql-client.md` (`shared/cloudsql_client.QueryBuilder`, `get_db`), `2026-08-25-agenda-plan-05-upsert-repository.md` (`apps.agenda.repository._como_datetime`), `2026-08-26-agenda-plan-09-api-consultas.md` (`apps.agenda.views._parse_data_iso`, convenções de erro/camelCase, `jwt_required`).

## Global Constraints

- **Convenção de JSON na borda HTTP: camelCase**, mesma de todo o serviço (Plano 09 Global Constraints) — resposta sempre `_serializar_*`, corpo/query interno sempre `snake_case`.
- **Forma do erro JSON, uniforme:** `{"erro": "<CODIGO>", "mensagem": "<texto>"}` (mesma convenção do Plano 09). Parâmetro de query string ausente/invalido → `400 CAMPO_OBRIGATORIO_AUSENTE` ou `400 PARAMETRO_INVALIDO`; falha inesperada → `500 ERRO_INTERNO`, logada com `logger.exception`.
- **`shared/cloudsql_client.py` ganha `.gt(field, value)`** (mesma forma de `.gte()`/`.lte()`, valida identificador, gera `campo > :pN`) **e `.group_by(*fields)`** (valida cada identificador como `.order()` já faz, gera `GROUP BY campo1, campo2` entre `WHERE` e `ORDER BY` em `_exec_select`). `.select(fields)` continua **sem guarda** (§10 do design doc já documenta isso) — expressões de agregação (`COALESCE(SUM(x),0) AS total`) são sempre escritas pelo código, nunca vêm de um parâmetro de query string.
- **Nenhum suporte a `JOIN`.** Onde uma agregação precisaria cruzar `agenda_ur` e `agenda_ur_pagamento` (Task 4), rodam duas queries independentes com os mesmos filtros de coluna compartilhados (`documento_ufr`, `data_liquidacao`, opcionalmente `cnpj_credenciadora`/`codigo_arranjo`) — nunca um `JOIN` de fato.
- **Paginação por cursor:**
  - `GET /agendas/urs` pagina sobre a nova coluna `agenda_ur.sequencia` (Task 2) — `BIGSERIAL`, populada só no `INSERT`, nunca tocada pelo `UPDATE` de upsert de frescor (`apps/agenda/repository.py::_upsert_cabecalho`), por isso permanece estável mesmo quando uma UR é atualizada em lugar.
  - `GET /compliance/relatorio` pagina sobre `consulta_agenda.id`, que já é um ULID (ordenável lexicograficamente por tempo de criação) — nenhuma coluna nova.
  - Cursor é o valor cru da última linha da página anterior (inteiro pra `agendas/urs`, string pra `compliance/relatorio`) — não um token opaco; API interna atrás de JWT, sem necessidade de ofuscar. `limit` default 100, máximo 1000 (design doc §10). Página cheia (`len(linhas) == limit + 1`, buscadas via "fetch limit+1, corta o extra") → `proximoCursor` não-nulo; página incompleta → `proximoCursor: null`.
- **`GET /agendas/urs/posicao` exige janela e UFR.** `ufr`, `dataLiquidacaoInicio`, `dataLiquidacaoFim` são obrigatórios (evita agregação sem filtro/full scan) — `credenciadora`/`arranjo` são filtros opcionais adicionais.
- **`valorFumaca` (`constituicao='2'`) nunca entra em `valorTotalConstituido`/`porCredenciadora`/`porArranjo`/`valorOnerado`** (SPEC03 §7.4, "regra de apresentação") — as três últimas quebras filtram `constituicao='1'` explicitamente; `valorFumaca` vem de uma consulta separada agrupada por `constituicao`. `porArranjo` devolve `codigoArranjo` cru, sem descrição — `dominio_arranjo` (design doc risco 11, sem seed) fica fora do escopo deste plano.
- **Valores monetários são sempre `Decimal`** (colunas `NUMERIC(18,2)`, nunca `float`) — `JsonResponse` usa `DjangoJSONEncoder` por padrão, que já serializa `decimal.Decimal` como string (`"150.00"`), então nenhum código de serialização extra é necessário pra cumprir "nenhum float em campo monetário" (SPEC03 §12.4).
- **Nomes de parâmetro de query string nunca viram nome de coluna diretamente** (§10 do design doc, achado da revisão final do Plano 03) — cada endpoint usa um dicionário fixo `{parâmetro: coluna}` (`_FILTROS_URS` na Task 3) para traduzir, nunca `request.GET.get(...)` direto num `.eq()`.
- **Risco 20 (N+1 de `GET /agendas/consultas/{id}`) não é tocado neste plano** — decisão explícita, ver Nota de série.

---

### Task 1: `.gt()` e `.group_by()` em `shared/cloudsql_client.py`

**Files:**
- Modify: `shared/cloudsql_client.py`
- Test: `shared/tests/test_cloudsql_client.py` (adiciona casos)

**Interfaces:**
- Produces: `QueryBuilder.gt(field: str, value) -> QueryBuilder` (filtro `>` estrito, mesma validação de identificador de `.eq()`/`.gte()`/`.lte()`); `QueryBuilder.group_by(*fields: str) -> QueryBuilder` (gera `GROUP BY`, valida cada identificador).

- [ ] **Step 1: Escrever os testes novos em `shared/tests/test_cloudsql_client.py`**

Adicionar ao final do arquivo:

```python
def test_gt_filter_excludes_equal_values():
    db = get_db(FINANCIADOR_TESTE)
    db.table("dominio_arranjo").delete().eq("codigo", "GT1").execute()
    db.table("dominio_arranjo").delete().eq("codigo", "GT2").execute()
    try:
        db.table("dominio_arranjo").insert({
            "codigo": "GT1", "descricao": "A", "ativo": True,
            "atualizado_em": "2026-01-01T00:00:00-03:00",
        }).execute()
        db.table("dominio_arranjo").insert({
            "codigo": "GT2", "descricao": "B", "ativo": True,
            "atualizado_em": "2026-06-01T00:00:00-03:00",
        }).execute()

        resultado = db.table("dominio_arranjo").select("*").gt(
            "atualizado_em", "2026-01-01T00:00:00-03:00"
        ).execute()
        codigos = {r["codigo"] for r in resultado.data}
        assert codigos == {"GT2"}  # estritamente maior — GT1 (valor igual) fica de fora
    finally:
        db.table("dominio_arranjo").delete().eq("codigo", "GT1").execute()
        db.table("dominio_arranjo").delete().eq("codigo", "GT2").execute()


def test_group_by_aggregates_with_sum():
    db = get_db(FINANCIADOR_TESTE)
    chave_base = {
        "entidade_registradora": "22246686000196",
        "documento_ufr": "TESTE-GROUPBY-UFR",
        "documento_titular": "TESTE-GROUPBY-UFR",
        "data_liquidacao": "2026-09-20",
    }
    linhas = [
        {**chave_base, "cnpj_credenciadora": "AAA", "codigo_arranjo": "VCC",
         "constituicao": "1", "valor_constituido_total": 100, "valor_total_ur": 100},
        {**chave_base, "cnpj_credenciadora": "AAA", "codigo_arranjo": "VCD",
         "constituicao": "1", "valor_constituido_total": 50, "valor_total_ur": 50},
        {**chave_base, "cnpj_credenciadora": "BBB", "codigo_arranjo": "VCC",
         "constituicao": "1", "valor_constituido_total": 30, "valor_total_ur": 30},
    ]

    def _limpar():
        for linha in linhas:
            db.table("agenda_ur").delete().eq("cnpj_credenciadora", linha["cnpj_credenciadora"]).eq(
                "codigo_arranjo", linha["codigo_arranjo"]
            ).eq("documento_ufr", chave_base["documento_ufr"]).eq(
                "data_liquidacao", chave_base["data_liquidacao"]
            ).execute()

    _limpar()
    try:
        for linha in linhas:
            db.table("agenda_ur").insert({
                **linha,
                "data_hora_ultima_atualizacao": "2026-09-19T10:00:00-03:00",
                "origem": "SINCRONO",
            }).execute()

        resultado = db.table("agenda_ur").select(
            "cnpj_credenciadora, COALESCE(SUM(valor_constituido_total),0) AS total"
        ).eq("documento_ufr", chave_base["documento_ufr"]).group_by("cnpj_credenciadora").order(
            "cnpj_credenciadora"
        ).execute()

        por_credenciadora = {r["cnpj_credenciadora"]: r["total"] for r in resultado.data}
        assert por_credenciadora["AAA"] == 150
        assert por_credenciadora["BBB"] == 30
    finally:
        _limpar()


def test_invalid_column_name_rejected_in_group_by():
    db = get_db(FINANCIADOR_TESTE)
    with pytest.raises(ValueError):
        db.table("dominio_arranjo").select("*").group_by("codigo; DROP TABLE dominio_arranjo; --")
```

- [ ] **Step 2: Rodar os testes novos e confirmar que falham**

Run: `pytest shared/tests/test_cloudsql_client.py -k "gt_filter or group_by" -v`
Expected: FAIL — `AttributeError: 'QueryBuilder' object has no attribute 'gt'` / `'group_by'`

- [ ] **Step 3: Implementar em `shared/cloudsql_client.py`**

No `__init__` de `QueryBuilder`, junto aos demais atributos de estado:

```python
        self._group_by: List[str] = []
```

Junto de `gte`/`lte` (logo depois de `lte`):

```python
    def gt(self, field: str, value: Any) -> "QueryBuilder":
        _validate_identifier(field, "coluna")
        self._filters.append(("gt", field, value))
        return self

    def group_by(self, *fields: str) -> "QueryBuilder":
        for field in fields:
            _validate_identifier(field, "coluna")
        self._group_by = list(fields)
        return self
```

Em `_build_where`, adicionar `"gt"` ao dicionário de operadores:

```python
        operadores = {"eq": "=", "gte": ">=", "lte": "<=", "gt": ">"}
```

Em `_exec_select`, montar a cláusula `GROUP BY` e inserir entre `WHERE` e `ORDER BY` (função inteira, com as duas linhas novas marcadas):

```python
    def _exec_select(self) -> ExecuteResult:
        from sqlalchemy import text

        where, params = self._build_where()
        with self._engine.connect() as conn:
            if self._count_mode == "exact":
                sql = f"SELECT COUNT(*) FROM {self._table} {where}"
                return ExecuteResult(data=[], count=conn.execute(text(sql), params).scalar())

            group_clause = f"GROUP BY {', '.join(self._group_by)}" if self._group_by else ""  # NOVA LINHA
            order_clause = ""
            if self._order_by:
                parts = [f"{f} {'DESC' if d else 'ASC'}" for f, d in self._order_by]
                order_clause = "ORDER BY " + ", ".join(parts)
            limit_clause = f"LIMIT {self._limit_val}" if self._limit_val else ""

            sql = f"SELECT {self._select_fields} FROM {self._table} {where} {group_clause} {order_clause} {limit_clause}"  # MODIFICADA
            result = conn.execute(text(sql), params)
            return ExecuteResult(data=[self._deserialize_row(dict(r._mapping)) for r in result])
```

- [ ] **Step 4: Rodar os testes e confirmar que passam**

Run: `pytest shared/tests/test_cloudsql_client.py -v`
Expected: PASS (todos, incluindo os 3 novos)

- [ ] **Step 5: Commit**

```bash
git add shared/cloudsql_client.py shared/tests/test_cloudsql_client.py
git commit -m "feat: adiciona .gt() e .group_by() ao QueryBuilder (Plano 10, Task 1)"
```

---

### Task 2: `sql/schema/05-agenda-ur-sequencia.sql` — coluna de cursor

**Files:**
- Create: `sql/schema/05-agenda-ur-sequencia.sql`

**Interfaces:**
- Produces: `agenda_ur.sequencia` (`BIGSERIAL`, indexada) — consumida pela Task 3.

- [ ] **Step 1: Escrever `sql/schema/05-agenda-ur-sequencia.sql`**

```sql
-- Coluna de cursor para paginação de GET /api/v1/agendas/urs (Plano 10,
-- design doc §10/§16): agenda_ur não tem coluna monotônica única (a chave
-- primária é composta de 6 colunas), então uma BIGSERIAL dedicada vira o
-- cursor de paginação — populada só no INSERT (nunca tocada pelo UPDATE
-- de upsert de frescor em apps/agenda/repository.py::_upsert_cabecalho),
-- permanecendo estável entre chamadas paginadas mesmo quando uma UR é
-- atualizada em lugar.

ALTER TABLE agenda_ur ADD COLUMN sequencia BIGSERIAL;
CREATE INDEX ON agenda_ur (sequencia);
```

- [ ] **Step 2: Aplicar no Cloud SQL real de dev**

Run: `python scripts/apply_schema.py sql/schema/05-agenda-ur-sequencia.sql`
Expected: `Aplicado sql/schema/05-agenda-ur-sequencia.sql: 2 statement(s).`

- [ ] **Step 3: Commit**

```bash
git add sql/schema/05-agenda-ur-sequencia.sql
git commit -m "feat: adiciona agenda_ur.sequencia como chave de cursor (Plano 10, Task 2)"
```

---

### Task 3: `GET /api/v1/agendas/urs`

**Files:**
- Modify: `apps/agenda/views.py` (novos imports; helpers `_parse_limite`/`_pagina_com_cursor`; nova view `listar_urs`)
- Modify: `apps/agenda/urls.py` (nova rota)
- Test: `apps/agenda/tests/test_views_listar_urs.py`

**Interfaces:**
- Consumes: `apps.agenda.repository._como_datetime` (Plano 05, já existe).
- Produces: `_parse_limite(request, maximo=1000) -> int` (levanta `ValueError`); `_pagina_com_cursor(query, campo_cursor: str, cursor, limite: int) -> tuple[list[dict], cursor|None]` — reutilizada pela Task 5. `GET /api/v1/agendas/urs?ufr=&titular=&credenciadora=&arranjo=&dataLiquidacaoInicio=&dataLiquidacaoFim=&constituicao=&origem=&atualizadoDesde=&cursor=&limit=` — `401` sem JWT; `400` (`PARAMETRO_INVALIDO`) `cursor`/`limit` malformado; sucesso `200` `{"urs": [...], "proximoCursor": <int|null>}`.

- [ ] **Step 1: Escrever `apps/agenda/tests/test_views_listar_urs.py`**

```python
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import Client

from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
UFR_TESTE = "22999888000177"
URL = "/api/v1/agendas/urs"


@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def _token(private_pem):
    payload = {"exp": int(time.time()) + 300, "iss": "brikz-iam", "sub": "analista@teste.com", "financiador_id": FINANCIADOR_TESTE}
    return pyjwt.encode(payload, private_pem, algorithm="RS256")


def _linha(codigo_arranjo: str, data_liquidacao: str):
    return {
        "entidade_registradora": "22246686000196",
        "cnpj_credenciadora": "36216798000150",
        "documento_ufr": UFR_TESTE,
        "documento_titular": UFR_TESTE,
        "codigo_arranjo": codigo_arranjo,
        "data_liquidacao": data_liquidacao,
        "constituicao": "1",
        "valor_constituido_total": 100,
        "valor_total_ur": 100,
        "data_hora_ultima_atualizacao": "2026-09-19T10:00:00-03:00",
        "origem": "SINCRONO",
    }


def _limpar():
    db = get_db(FINANCIADOR_TESTE)
    db.table("agenda_ur").delete().eq("documento_ufr", UFR_TESTE).execute()


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch, keypair):
    _, public_pem = keypair
    monkeypatch.setenv("IAM_JWT_PUBLIC_KEY", public_pem)
    monkeypatch.setenv("IAM_JWT_ISSUER", "brikz-iam")
    _limpar()
    yield
    _limpar()


def test_sem_jwt_retorna_401():
    assert Client().get(URL).status_code == 401


def test_lista_filtrada_por_ufr(keypair):
    private_pem, _ = keypair
    db = get_db(FINANCIADOR_TESTE)
    db.table("agenda_ur").insert(_linha("VCC", "2026-09-20")).execute()
    db.table("agenda_ur").insert(_linha("VCD", "2026-09-21")).execute()

    response = Client().get(f"{URL}?ufr={UFR_TESTE}", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}")
    assert response.status_code == 200
    corpo = response.json()
    assert len(corpo["urs"]) == 2
    assert corpo["proximoCursor"] is None
    assert {ur["codigoArranjo"] for ur in corpo["urs"]} == {"VCC", "VCD"}
    assert "sequencia" not in corpo["urs"][0]


def test_paginacao_por_cursor(keypair):
    private_pem, _ = keypair
    db = get_db(FINANCIADOR_TESTE)
    db.table("agenda_ur").insert(_linha("VCC", "2026-09-20")).execute()
    db.table("agenda_ur").insert(_linha("VCD", "2026-09-21")).execute()

    primeira = Client().get(f"{URL}?ufr={UFR_TESTE}&limit=1", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}")
    assert primeira.status_code == 200
    corpo1 = primeira.json()
    assert len(corpo1["urs"]) == 1
    assert corpo1["proximoCursor"] is not None

    segunda = Client().get(
        f"{URL}?ufr={UFR_TESTE}&limit=1&cursor={corpo1['proximoCursor']}",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    corpo2 = segunda.json()
    assert len(corpo2["urs"]) == 1
    assert corpo2["urs"][0]["codigoArranjo"] != corpo1["urs"][0]["codigoArranjo"]
    assert corpo2["proximoCursor"] is None


def test_cursor_invalido_retorna_400(keypair):
    private_pem, _ = keypair
    response = Client().get(f"{URL}?cursor=abc", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}")
    assert response.status_code == 400
    assert response.json()["erro"] == "PARAMETRO_INVALIDO"


def test_limit_acima_do_maximo_retorna_400(keypair):
    private_pem, _ = keypair
    response = Client().get(f"{URL}?limit=1001", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}")
    assert response.status_code == 400
    assert response.json()["erro"] == "PARAMETRO_INVALIDO"
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `pytest apps/agenda/tests/test_views_listar_urs.py -v`
Expected: FAIL — rota `agendas/urs` não existe (404)

- [ ] **Step 3: Adicionar os helpers e a view em `apps/agenda/views.py`**

No final do arquivo:

```python
_LIMITE_PADRAO_LISTAGEM = 100
_LIMITE_MAXIMO_LISTAGEM = 1000

_FILTROS_URS = {
    "ufr": "documento_ufr", "titular": "documento_titular",
    "credenciadora": "cnpj_credenciadora", "arranjo": "codigo_arranjo",
    "constituicao": "constituicao", "origem": "origem",
}


def _parse_limite(request, maximo: int = _LIMITE_MAXIMO_LISTAGEM) -> int:
    bruto = request.GET.get("limit")
    if not bruto:
        return _LIMITE_PADRAO_LISTAGEM
    try:
        valor = int(bruto)
    except ValueError:
        raise ValueError(f"limit deve ser um inteiro: {bruto!r}")
    if valor <= 0 or valor > maximo:
        raise ValueError(f"limit deve estar entre 1 e {maximo}: {valor}")
    return valor


def _pagina_com_cursor(query, campo_cursor: str, cursor, limite: int) -> tuple:
    """Keyset pagination genérica: busca limite+1 linhas ordenadas por
    campo_cursor, corta a linha extra e usa o cursor dela como
    proximoCursor — evita um SELECT COUNT(*) só pra saber se há mais
    página. Reaproveitada por listar_urs (Task 3) e relatorio_compliance
    (Task 5)."""
    if cursor is not None:
        query = query.gt(campo_cursor, cursor)
    linhas = query.order(campo_cursor).limit(limite + 1).execute().data
    tem_mais = len(linhas) > limite
    pagina = linhas[:limite]
    proximo_cursor = pagina[-1][campo_cursor] if tem_mais and pagina else None
    return pagina, proximo_cursor


def _serializar_ur(ur: dict) -> dict:
    return {
        "entidadeRegistradora": ur["entidade_registradora"],
        "cnpjCredenciadora": ur["cnpj_credenciadora"],
        "documentoUfr": ur["documento_ufr"],
        "documentoTitular": ur["documento_titular"],
        "codigoArranjo": ur["codigo_arranjo"],
        "dataLiquidacao": ur["data_liquidacao"].isoformat(),
        "constituicao": ur["constituicao"],
        "valorConstituidoTotal": ur["valor_constituido_total"],
        "valorConstituidoAntecipacaoPre": ur["valor_constituido_antecipacao_pre"],
        "valorBloqueado": ur["valor_bloqueado"],
        "valorLivre": ur["valor_livre"],
        "valorTotalUR": ur["valor_total_ur"],
        "carteira": ur["carteira"],
        "dataHoraUltimaAtualizacao": _como_datetime(ur["data_hora_ultima_atualizacao"]).isoformat(),
        "origem": ur["origem"],
        "origemArquivo": ur["origem_arquivo"],
    }


@jwt_required
@require_GET
def listar_urs(request):
    """GET /api/v1/agendas/urs (design doc §10, SPEC03 §7.3) — repositório
    consolidado, não chama a CERC. Paginação por cursor sobre
    agenda_ur.sequencia (Plano 10, Global Constraints)."""
    try:
        cursor_bruto = request.GET.get("cursor")
        cursor = int(cursor_bruto) if cursor_bruto else None
        limite = _parse_limite(request)
    except ValueError as exc:
        return JsonResponse({"erro": "PARAMETRO_INVALIDO", "mensagem": str(exc)}, status=400)

    try:
        db = get_db(request.financiador_id)
        query = db.table("agenda_ur").select("*")
        for parametro, coluna in _FILTROS_URS.items():
            valor = request.GET.get(parametro)
            if valor:
                query = query.eq(coluna, valor)

        data_inicio = request.GET.get("dataLiquidacaoInicio")
        data_fim = request.GET.get("dataLiquidacaoFim")
        atualizado_desde = request.GET.get("atualizadoDesde")
        if data_inicio:
            query = query.gte("data_liquidacao", data_inicio)
        if data_fim:
            query = query.lte("data_liquidacao", data_fim)
        if atualizado_desde:
            query = query.gte("atualizado_em", atualizado_desde)

        pagina, proximo_cursor = _pagina_com_cursor(query, "sequencia", cursor, limite)
        return JsonResponse(
            {"urs": [_serializar_ur(ur) for ur in pagina], "proximoCursor": proximo_cursor}, status=200,
        )
    except Exception:
        logger.exception("[AgendasUrs] Falha inesperada ao listar (financiador=%s)", request.financiador_id)
        return JsonResponse({"erro": "ERRO_INTERNO", "mensagem": "falha inesperada ao listar URs"}, status=500)
```

- [ ] **Step 4: Registrar a rota em `apps/agenda/urls.py`**

```python
    path("agendas/urs", views.listar_urs),
```

- [ ] **Step 5: Rodar os testes e confirmar que passam**

Run: `pytest apps/agenda/tests/test_views_listar_urs.py -v`
Expected: PASS (5 testes)

- [ ] **Step 6: Commit**

```bash
git add apps/agenda/views.py apps/agenda/urls.py apps/agenda/tests/test_views_listar_urs.py
git commit -m "feat: endpoint GET /api/v1/agendas/urs (Plano 10, Task 3)"
```

---

### Task 4: `GET /api/v1/agendas/urs/posicao`

**Files:**
- Modify: `apps/agenda/views.py` (nova view `posicao_urs`)
- Modify: `apps/agenda/urls.py` (nova rota)
- Test: `apps/agenda/tests/test_views_posicao_urs.py`

**Interfaces:**
- Produces: `GET /api/v1/agendas/urs/posicao?ufr=&dataLiquidacaoInicio=&dataLiquidacaoFim=&credenciadora=&arranjo=` — `401` sem JWT; `400` (`CAMPO_OBRIGATORIO_AUSENTE`) faltando `ufr`/`dataLiquidacaoInicio`/`dataLiquidacaoFim`; sucesso `200` `{"valorTotalConstituido", "valorLivre", "valorBloqueado", "valorOnerado", "valorFumaca", "porCredenciadora": [{"cnpjCredenciadora", "valorTotalConstituido"}], "porArranjo": [{"codigoArranjo", "valorTotalConstituido"}], "frescor": {"maisAntigo", "maisRecente"} | null}` (SPEC03 §7.4).

- [ ] **Step 1: Escrever `apps/agenda/tests/test_views_posicao_urs.py`**

```python
import time
from decimal import Decimal

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import Client

from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
UFR_TESTE = "22888777000166"
URL = "/api/v1/agendas/urs/posicao"


@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def _token(private_pem):
    payload = {"exp": int(time.time()) + 300, "iss": "brikz-iam", "sub": "analista@teste.com", "financiador_id": FINANCIADOR_TESTE}
    return pyjwt.encode(payload, private_pem, algorithm="RS256")


def _linha_ur(cnpj_credenciadora: str, codigo_arranjo: str, constituicao: str, valor_total: float, valor_bloqueado=0, valor_livre=0):
    return {
        "entidade_registradora": "22246686000196",
        "cnpj_credenciadora": cnpj_credenciadora,
        "documento_ufr": UFR_TESTE,
        "documento_titular": UFR_TESTE,
        "codigo_arranjo": codigo_arranjo,
        "data_liquidacao": "2026-09-20",
        "constituicao": constituicao,
        "valor_constituido_total": valor_total,
        "valor_bloqueado": valor_bloqueado,
        "valor_livre": valor_livre,
        "valor_total_ur": valor_total,
        "data_hora_ultima_atualizacao": "2026-09-19T10:00:00-03:00",
        "origem": "SINCRONO",
    }


def _limpar():
    db = get_db(FINANCIADOR_TESTE)
    db.table("agenda_ur_pagamento").delete().eq("documento_ufr", UFR_TESTE).execute()
    db.table("agenda_ur").delete().eq("documento_ufr", UFR_TESTE).execute()


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch, keypair):
    _, public_pem = keypair
    monkeypatch.setenv("IAM_JWT_PUBLIC_KEY", public_pem)
    monkeypatch.setenv("IAM_JWT_ISSUER", "brikz-iam")
    _limpar()
    yield
    _limpar()


def test_sem_jwt_retorna_401():
    assert Client().get(URL).status_code == 401


def test_parametro_obrigatorio_ausente_retorna_400(keypair):
    private_pem, _ = keypair
    response = Client().get(f"{URL}?ufr={UFR_TESTE}", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}")
    assert response.status_code == 400
    assert response.json()["erro"] == "CAMPO_OBRIGATORIO_AUSENTE"


def test_agrega_por_credenciadora_arranjo_e_segrega_fumaca(keypair):
    private_pem, _ = keypair
    db = get_db(FINANCIADOR_TESTE)
    db.table("agenda_ur").insert(_linha_ur("AAA", "VCC", "1", 100, valor_bloqueado=20, valor_livre=80)).execute()
    db.table("agenda_ur").insert(_linha_ur("AAA", "VCD", "1", 50, valor_bloqueado=0, valor_livre=50)).execute()
    db.table("agenda_ur").insert(_linha_ur("BBB", "VCC", "2", 999, valor_bloqueado=0, valor_livre=0)).execute()  # fumaça

    response = Client().get(
        f"{URL}?ufr={UFR_TESTE}&dataLiquidacaoInicio=2026-09-01&dataLiquidacaoFim=2026-09-30",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 200
    corpo = response.json()

    assert Decimal(corpo["valorTotalConstituido"]) == Decimal("150.00")
    assert Decimal(corpo["valorBloqueado"]) == Decimal("20.00")
    assert Decimal(corpo["valorLivre"]) == Decimal("130.00")
    assert Decimal(corpo["valorFumaca"]) == Decimal("999.00")

    por_credenciadora = {p["cnpjCredenciadora"]: Decimal(p["valorTotalConstituido"]) for p in corpo["porCredenciadora"]}
    assert por_credenciadora == {"AAA": Decimal("150.00")}  # BBB é só fumaça — não aparece aqui

    por_arranjo = {p["codigoArranjo"]: Decimal(p["valorTotalConstituido"]) for p in corpo["porArranjo"]}
    assert por_arranjo == {"VCC": Decimal("100.00"), "VCD": Decimal("50.00")}

    assert corpo["frescor"]["maisAntigo"] is not None
    assert corpo["frescor"]["maisRecente"] is not None


def test_sem_urs_no_periodo_retorna_zeros_e_frescor_none(keypair):
    private_pem, _ = keypair
    response = Client().get(
        f"{URL}?ufr={UFR_TESTE}&dataLiquidacaoInicio=2026-01-01&dataLiquidacaoFim=2026-01-31",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 200
    corpo = response.json()
    assert Decimal(corpo["valorTotalConstituido"]) == Decimal("0")
    assert corpo["porCredenciadora"] == []
    assert corpo["porArranjo"] == []
    assert corpo["frescor"] is None
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `pytest apps/agenda/tests/test_views_posicao_urs.py -v`
Expected: FAIL — rota não existe

- [ ] **Step 3: Adicionar a view em `apps/agenda/views.py`**

No final do arquivo:

```python
_CAMPOS_OBRIGATORIOS_POSICAO = ("ufr", "dataLiquidacaoInicio", "dataLiquidacaoFim")


def _aplicar_filtros_posicao(query, request):
    query = query.eq("documento_ufr", request.GET["ufr"])
    query = query.gte("data_liquidacao", request.GET["dataLiquidacaoInicio"])
    query = query.lte("data_liquidacao", request.GET["dataLiquidacaoFim"])
    credenciadora = request.GET.get("credenciadora")
    arranjo = request.GET.get("arranjo")
    if credenciadora:
        query = query.eq("cnpj_credenciadora", credenciadora)
    if arranjo:
        query = query.eq("codigo_arranjo", arranjo)
    return query


@jwt_required
@require_GET
def posicao_urs(request):
    """GET /api/v1/agendas/urs/posicao (design doc §10, SPEC03 §7.4) —
    visão agregada de crédito por UFR/janela. valorFumaca (constituicao=2)
    é sempre segregado: nunca soma em valorTotalConstituido/porCredenciadora
    /porArranjo/valorOnerado (Global Constraints deste plano). Sem JOIN:
    valorOnerado roda como query separada sobre agenda_ur_pagamento com os
    mesmos filtros de coluna compartilhados."""
    faltando = [c for c in _CAMPOS_OBRIGATORIOS_POSICAO if not request.GET.get(c)]
    if faltando:
        return JsonResponse(
            {"erro": "CAMPO_OBRIGATORIO_AUSENTE", "mensagem": f"parâmetros obrigatórios ausentes: {', '.join(faltando)}"},
            status=400,
        )

    try:
        db = get_db(request.financiador_id)

        por_constituicao = _aplicar_filtros_posicao(
            db.table("agenda_ur").select(
                "constituicao, COALESCE(SUM(valor_constituido_total),0) AS total, "
                "COALESCE(SUM(valor_bloqueado),0) AS bloqueado, COALESCE(SUM(valor_livre),0) AS livre"
            ),
            request,
        ).group_by("constituicao").execute().data
        totais = {linha["constituicao"]: linha for linha in por_constituicao}
        constituido = totais.get("1", {"total": 0, "bloqueado": 0, "livre": 0})
        fumaca = totais.get("2", {"total": 0})

        por_credenciadora = _aplicar_filtros_posicao(
            db.table("agenda_ur").select(
                "cnpj_credenciadora, COALESCE(SUM(valor_constituido_total),0) AS total"
            ).eq("constituicao", "1"),
            request,
        ).group_by("cnpj_credenciadora").execute().data

        por_arranjo = _aplicar_filtros_posicao(
            db.table("agenda_ur").select(
                "codigo_arranjo, COALESCE(SUM(valor_constituido_total),0) AS total"
            ).eq("constituicao", "1"),
            request,
        ).group_by("codigo_arranjo").execute().data

        onerado = _aplicar_filtros_posicao(
            db.table("agenda_ur_pagamento").select("COALESCE(SUM(valor_onerado),0) AS total"), request,
        ).execute().data
        valor_onerado = onerado[0]["total"]

        frescor_linhas = _aplicar_filtros_posicao(
            db.table("agenda_ur").select(
                "MIN(data_hora_ultima_atualizacao) AS mais_antigo, MAX(data_hora_ultima_atualizacao) AS mais_recente"
            ),
            request,
        ).execute().data
        frescor = None
        if frescor_linhas and frescor_linhas[0]["mais_antigo"] is not None:
            frescor = {
                "maisAntigo": _como_datetime(frescor_linhas[0]["mais_antigo"]).isoformat(),
                "maisRecente": _como_datetime(frescor_linhas[0]["mais_recente"]).isoformat(),
            }

        return JsonResponse({
            "valorTotalConstituido": constituido["total"],
            "valorLivre": constituido["livre"],
            "valorBloqueado": constituido["bloqueado"],
            "valorOnerado": valor_onerado,
            "valorFumaca": fumaca["total"],
            "porCredenciadora": [
                {"cnpjCredenciadora": l["cnpj_credenciadora"], "valorTotalConstituido": l["total"]}
                for l in por_credenciadora
            ],
            "porArranjo": [
                {"codigoArranjo": l["codigo_arranjo"], "valorTotalConstituido": l["total"]}
                for l in por_arranjo
            ],
            "frescor": frescor,
        }, status=200)
    except Exception:
        logger.exception("[AgendasUrsPosicao] Falha inesperada (financiador=%s)", request.financiador_id)
        return JsonResponse({"erro": "ERRO_INTERNO", "mensagem": "falha inesperada ao calcular posição"}, status=500)
```

- [ ] **Step 4: Registrar a rota em `apps/agenda/urls.py`**

```python
    path("agendas/urs/posicao", views.posicao_urs),
```

- [ ] **Step 5: Rodar os testes e confirmar que passam**

Run: `pytest apps/agenda/tests/test_views_posicao_urs.py -v`
Expected: PASS (4 testes)

- [ ] **Step 6: Commit**

```bash
git add apps/agenda/views.py apps/agenda/urls.py apps/agenda/tests/test_views_posicao_urs.py
git commit -m "feat: endpoint GET /api/v1/agendas/urs/posicao (Plano 10, Task 4)"
```

---

### Task 5: `GET /api/v1/compliance/relatorio`

**Files:**
- Modify: `apps/agenda/views.py` (nova view `relatorio_compliance`)
- Modify: `apps/agenda/urls.py` (nova rota)
- Test: `apps/agenda/tests/test_views_relatorio_compliance.py`

**Interfaces:**
- Consumes: `_pagina_com_cursor` (Task 3), `_parse_data_iso`, `_parse_limite` (já existentes/Task 3).
- Produces: `GET /api/v1/compliance/relatorio?dataInicio=&dataFim=&ufr=&ator=&cursor=&limit=` — `401` sem JWT; `400` (`CAMPO_OBRIGATORIO_AUSENTE`) faltando `dataInicio`/`dataFim`; `400` (`PARAMETRO_INVALIDO`) data/limite malformado; sucesso `200` `{"consultas": [...trilha completa camelCase...], "proximoCursor": <string|null>}`.

- [ ] **Step 1: Escrever `apps/agenda/tests/test_views_relatorio_compliance.py`**

```python
import time
from datetime import date, datetime, timezone

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import Client
from ulid import ULID

from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
UFR_TESTE = "22777666000155"
URL = "/api/v1/compliance/relatorio"


@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def _token(private_pem):
    payload = {"exp": int(time.time()) + 300, "iss": "brikz-iam", "sub": "analista@teste.com", "financiador_id": FINANCIADOR_TESTE}
    return pyjwt.encode(payload, private_pem, algorithm="RS256")


def _consulta(ator: str, iniciada_em: str):
    return {
        "id": str(ULID()),
        "modo": "BATCH", "status": "COMPLETA",
        "filtro_ufr": UFR_TESTE, "filtro_titular": None,
        "filtro_credenciadoras": ["99T"], "filtro_arranjos": ["99T"],
        "filtro_data_inicio": "2026-09-01", "filtro_data_fim": "2026-09-30",
        "base_autorizativa_tipo": "OPTIN", "base_autorizativa_id": "opt_1",
        "motivo": "TESTE-RELATORIO", "ator": ator, "origem_ip": "127.0.0.1",
        "iniciada_em": iniciada_em, "encerrada_em": iniciada_em,
    }


def _limpar():
    get_db(FINANCIADOR_TESTE).table("consulta_agenda").delete().eq("filtro_ufr", UFR_TESTE).execute()


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch, keypair):
    _, public_pem = keypair
    monkeypatch.setenv("IAM_JWT_PUBLIC_KEY", public_pem)
    monkeypatch.setenv("IAM_JWT_ISSUER", "brikz-iam")
    _limpar()
    yield
    _limpar()


def test_sem_jwt_retorna_401():
    assert Client().get(URL).status_code == 401


def test_parametro_obrigatorio_ausente_retorna_400(keypair):
    private_pem, _ = keypair
    response = Client().get(URL, HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}")
    assert response.status_code == 400
    assert response.json()["erro"] == "CAMPO_OBRIGATORIO_AUSENTE"


def test_filtra_por_periodo_ufr_e_ator(keypair):
    private_pem, _ = keypair
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta("analista.a@teste.com", "2026-09-10T10:00:00-03:00")).execute()
    db.table("consulta_agenda").insert(_consulta("analista.b@teste.com", "2026-09-15T10:00:00-03:00")).execute()
    db.table("consulta_agenda").insert(_consulta("analista.a@teste.com", "2026-08-01T10:00:00-03:00")).execute()  # fora do período

    response = Client().get(
        f"{URL}?dataInicio=2026-09-01&dataFim=2026-09-30&ufr={UFR_TESTE}",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 200
    corpo = response.json()
    assert len(corpo["consultas"]) == 2
    assert {c["ator"] for c in corpo["consultas"]} == {"analista.a@teste.com", "analista.b@teste.com"}
    assert corpo["consultas"][0]["origemIp"] == "127.0.0.1"
    assert corpo["consultas"][0]["baseAutorizativaTipo"] == "OPTIN"

    filtrado_por_ator = Client().get(
        f"{URL}?dataInicio=2026-09-01&dataFim=2026-09-30&ufr={UFR_TESTE}&ator=analista.a@teste.com",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    corpo_ator = filtrado_por_ator.json()
    assert len(corpo_ator["consultas"]) == 1
    assert corpo_ator["consultas"][0]["ator"] == "analista.a@teste.com"


def test_paginacao_por_cursor(keypair):
    private_pem, _ = keypair
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta("analista.a@teste.com", "2026-09-10T10:00:00-03:00")).execute()
    db.table("consulta_agenda").insert(_consulta("analista.b@teste.com", "2026-09-15T10:00:00-03:00")).execute()

    primeira = Client().get(
        f"{URL}?dataInicio=2026-09-01&dataFim=2026-09-30&ufr={UFR_TESTE}&limit=1",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    corpo1 = primeira.json()
    assert len(corpo1["consultas"]) == 1
    assert corpo1["proximoCursor"] is not None

    segunda = Client().get(
        f"{URL}?dataInicio=2026-09-01&dataFim=2026-09-30&ufr={UFR_TESTE}&limit=1&cursor={corpo1['proximoCursor']}",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    corpo2 = segunda.json()
    assert len(corpo2["consultas"]) == 1
    assert corpo2["consultas"][0]["consultaId"] != corpo1["consultas"][0]["consultaId"]
    assert corpo2["proximoCursor"] is None


def test_data_invalida_retorna_400(keypair):
    private_pem, _ = keypair
    response = Client().get(
        f"{URL}?dataInicio=nao-e-uma-data&dataFim=2026-09-30", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 400
    assert response.json()["erro"] == "PARAMETRO_INVALIDO"
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `pytest apps/agenda/tests/test_views_relatorio_compliance.py -v`
Expected: FAIL — rota não existe

- [ ] **Step 3: Adicionar a view em `apps/agenda/views.py`**

No final do arquivo:

```python
def _serializar_consulta_compliance(consulta: dict) -> dict:
    return {
        "consultaId": consulta["id"],
        "modo": consulta["modo"],
        "status": consulta["status"],
        "filtroUfr": consulta["filtro_ufr"],
        "filtroTitular": consulta["filtro_titular"],
        "filtroCredenciadoras": consulta["filtro_credenciadoras"],
        "filtroArranjos": consulta["filtro_arranjos"],
        "filtroDataInicio": consulta["filtro_data_inicio"].isoformat(),
        "filtroDataFim": consulta["filtro_data_fim"].isoformat(),
        "tipoAvaliacao": consulta["tipo_avaliacao"],
        "carteira": consulta["carteira"],
        "baseAutorizativaTipo": consulta["base_autorizativa_tipo"],
        "baseAutorizativaId": consulta["base_autorizativa_id"],
        "motivo": consulta["motivo"],
        "ator": consulta["ator"],
        "origemIp": consulta["origem_ip"],
        "qtdUrsSincrono": consulta["qtd_urs_sincrono"],
        "qtdUrsWebhook": consulta["qtd_urs_webhook"],
        "iniciadaEm": consulta["iniciada_em"].isoformat(),
        "ultimaUrEm": consulta["ultima_ur_em"].isoformat() if consulta["ultima_ur_em"] else None,
        "encerradaEm": consulta["encerrada_em"].isoformat() if consulta["encerrada_em"] else None,
    }


_CAMPOS_OBRIGATORIOS_RELATORIO = ("dataInicio", "dataFim")


@jwt_required
@require_GET
def relatorio_compliance(request):
    """GET /api/v1/compliance/relatorio (design doc §10/§11, SPEC03 §8
    item 5) — trilha de compliance completa, síncrona e paginada por
    cursor sobre consulta_agenda.id (ULID, já ordenável por tempo de
    criação — Global Constraints deste plano, sem coluna nova). Sem
    exportação CSV/job assíncrono — design doc §11 explicitamente adia
    isso pra quando houver necessidade comprovada."""
    faltando = [c for c in _CAMPOS_OBRIGATORIOS_RELATORIO if not request.GET.get(c)]
    if faltando:
        return JsonResponse(
            {"erro": "CAMPO_OBRIGATORIO_AUSENTE", "mensagem": f"parâmetros obrigatórios ausentes: {', '.join(faltando)}"},
            status=400,
        )

    try:
        data_inicio = _parse_data_iso(request.GET.get("dataInicio"), "dataInicio")
        data_fim = _parse_data_iso(request.GET.get("dataFim"), "dataFim")
        limite = _parse_limite(request)
    except ValueError as exc:
        return JsonResponse({"erro": "PARAMETRO_INVALIDO", "mensagem": str(exc)}, status=400)

    # Dia calendário em America/Sao_Paulo, não UTC (mesma convenção de
    # apps.agenda.validation._FUSO_CONSULTA/A08) — janela [início, fim+1dia)
    # tratada como fechada no fim via -1 microssegundo, já que QueryBuilder
    # não tem operador "menor que" (só gte/lte/gt).
    inicio_dt = datetime.combine(data_inicio, datetime.min.time(), tzinfo=_FUSO_CONSULTA)
    fim_dt = (
        datetime.combine(data_fim + timedelta(days=1), datetime.min.time(), tzinfo=_FUSO_CONSULTA)
        - timedelta(microseconds=1)
    )
    cursor = request.GET.get("cursor") or None

    try:
        db = get_db(request.financiador_id)
        query = db.table("consulta_agenda").select("*").gte("iniciada_em", inicio_dt).lte("iniciada_em", fim_dt)
        ufr = request.GET.get("ufr")
        ator = request.GET.get("ator")
        if ufr:
            query = query.eq("filtro_ufr", ufr)
        if ator:
            query = query.eq("ator", ator)

        pagina, proximo_cursor = _pagina_com_cursor(query, "id", cursor, limite)
        return JsonResponse(
            {"consultas": [_serializar_consulta_compliance(c) for c in pagina], "proximoCursor": proximo_cursor},
            status=200,
        )
    except Exception:
        logger.exception("[ComplianceRelatorio] Falha inesperada (financiador=%s)", request.financiador_id)
        return JsonResponse({"erro": "ERRO_INTERNO", "mensagem": "falha inesperada ao gerar relatório"}, status=500)
```

- [ ] **Step 4: Registrar a rota em `apps/agenda/urls.py`**

```python
    path("compliance/relatorio", views.relatorio_compliance),
```

- [ ] **Step 5: Rodar os testes e confirmar que passam**

Run: `pytest apps/agenda/tests/test_views_relatorio_compliance.py -v`
Expected: PASS (5 testes)

- [ ] **Step 6: Rodar a suíte inteira do serviço pra checar regressão**

Run: `pytest apps/agenda services/cerc shared -v`
Expected: PASS em todos os testes (Planos 01–10)

- [ ] **Step 7: Commit**

```bash
git add apps/agenda/views.py apps/agenda/urls.py apps/agenda/tests/test_views_relatorio_compliance.py
git commit -m "feat: endpoint GET /api/v1/compliance/relatorio (Plano 10, Task 5)"
```
