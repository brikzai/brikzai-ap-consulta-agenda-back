# agenda-service — Plan 05: Repositório de Upsert de `agenda_ur` (Frescor + Histórico de Eventos) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dar ao serviço o ponto único de escrita de uma UR — `apps/agenda/repository.py` — que aplica a regra de frescor/precedência (design doc §8) ao fazer upsert de `agenda_ur`/`agenda_ur_pagamento`, remove linhas de pagamento obsoletas (§15 risco 7) e grava uma trilha de eventos em `agenda_ur_evento` (Captura/Bloqueio/Disponibilização/Liquidação — §15 risco 12) para alimentar a timeline que o front (`ap-front/src/components/ScheduleModal.tsx`) já exibe.

**Architecture:** Duas tasks. Task 1 cria a tabela `agenda_ur_evento` (não existe ainda — decisão registrada no design doc §15 risco 12) e aplica no Cloud SQL real de dev, mesmo mecanismo do Plano 02. Task 2 escreve `apps/agenda/repository.py`: uma única função `upsert_agenda_ur(financiador_id, cabecalho, pagamentos)` que os Planos 06 (CERC síncrono), 07 (webhook) e 08 (arquivo AP005) vão chamar — cada um só traduz sua origem para o mesmo formato de entrada, a decisão de frescor/limpeza/eventos mora aqui uma única vez.

**Tech Stack:** `python-ulid` (já em `requirements.txt`, ainda não usado por nenhum plano anterior) para os ids de `agenda_ur_evento`. Nenhuma dependência nova. Usa `shared.cloudsql_client.get_db` (Plano 03) sem modificá-lo.

**Spec:** `docs/superpowers/specs/2026-08-24-agenda-service-design.md` §8 (regra de upsert/frescor), §15 riscos 1 (decisão de upsert não-atômico), 7 (limpeza de pagamento obsoleto) e 12 (histórico de eventos). `docs/specs/SPEC-03-consulta-de-agenda-ap005.md` §5.4/§9. `docs/specs/SPEC-04-modelo-de-dados.md` §5.3 (DDL de referência — este plano usa a versão simplificada fase-1 já aplicada em `sql/schema/01-agenda-schema.sql`, sem particionamento). Série: plano 5 de ~10.

**Depends on:** `2026-08-24-agenda-plan-02-schema.md` (schema aplicado), `2026-08-24-agenda-plan-03-cloudsql-client.md` (`shared.cloudsql_client.get_db`).

## Global Constraints

- Sem particionamento nesta fase — mesma decisão já tomada para `agenda_ur`/`agenda_ur_pagamento` (design doc §2, §15 risco 2). `agenda_ur_evento` segue a mesma convenção fase-1.
- `pagamentos` passado a `upsert_agenda_ur` deve ser a lista **completa e atual** de efeitos da UR neste lote, nunca um delta — é essa premissa que permite decidir quais linhas antigas ficaram obsoletas (§15 risco 7). Registrar isso explicitamente para os Planos 06/07/08 quando forem escritos.
- `cabecalho["data_hora_ultima_atualizacao"]` deve ser um `datetime.datetime` com `tzinfo` (já parseado do RFC3339 da CERC) — parsear a string é responsabilidade do chamador (Plano 06/07/08), não deste repositório.
- Decisão do §15 risco 1: opção (b) — `SELECT` + comparação em Python, não upsert atômico via `ON CONFLICT`. `shared/cloudsql_client.py` **não** é modificado por este plano (a limpeza de pagamento obsoleto usa só `.eq()`/`.select()`/`.delete()` já existentes, comparando chaves em Python — não precisa de um operador `.lt()`/`.ne()` novo no query builder).
- Nomenclatura de origem: `'SINCRONO'|'WEBHOOK'|'ARQUIVO'` (mesmos valores do `CHECK` de `agenda_ur.origem`, design doc §8).

---

### Task 1: `sql/schema/03-agenda-ur-evento.sql`

**Files:**
- Create: `sql/schema/03-agenda-ur-evento.sql`

**Interfaces:**
- Produces: tabela `agenda_ur_evento` (colunas: `id`, chave da UR, `tipo_evento`, `origem`, `valor`, `ocorrido_em`, `registrado_em`) — consumida por `apps/agenda/repository.py` (Task 2) e, futuramente, pela API de consulta de timeline (Plano 09).

- [ ] **Step 1: Escrever `sql/schema/03-agenda-ur-evento.sql`**

```sql
-- Histórico de eventos por UR (design doc §15 risco 12) — a timeline que
-- ap-front/ScheduleModal.tsx exibe ("Captura", "Bloqueio",
-- "Disponibilização", "Liquidação") não tinha tabela própria: agenda_ur só
-- guarda o estado atual. Append-only, sem particionamento nesta fase
-- (mesma decisão já aceita para agenda_ur/agenda_ur_pagamento, §2).

CREATE TABLE agenda_ur_evento (
  id                     TEXT PRIMARY KEY,          -- ULID, gerado pela aplicação
  entidade_registradora  TEXT NOT NULL,
  cnpj_credenciadora     TEXT NOT NULL,
  documento_ufr          TEXT NOT NULL,
  documento_titular      TEXT NOT NULL,
  codigo_arranjo         TEXT NOT NULL,
  data_liquidacao        DATE NOT NULL,
  tipo_evento            TEXT NOT NULL,             -- CAPTURA|BLOQUEIO|DISPONIBILIZACAO|LIQUIDACAO
  origem                 TEXT NOT NULL,              -- SINCRONO|WEBHOOK|ARQUIVO
  valor                  NUMERIC(18,2) NOT NULL,
  ocorrido_em            TIMESTAMPTZ NOT NULL,        -- data_hora_ultima_atualizacao do lote que gerou o evento
  registrado_em          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON agenda_ur_evento (documento_ufr, data_liquidacao, ocorrido_em);
CREATE INDEX ON agenda_ur_evento (
  entidade_registradora, cnpj_credenciadora, documento_ufr,
  documento_titular, codigo_arranjo, data_liquidacao, ocorrido_em
);
```

- [ ] **Step 2: Aplicar no Cloud SQL real de dev**

Run: `python scripts/apply_schema.py sql/schema/03-agenda-ur-evento.sql`
Expected: saída `Aplicado sql/schema/03-agenda-ur-evento.sql: N statement(s).` (N = 3: `CREATE TABLE` + 2 `CREATE INDEX`). Se já tiver sido aplicado antes com o mesmo conteúdo, a saída é `já aplicado (checksum igual), pulando.` — também esperado, não é falha.

- [ ] **Step 3: Confirmar que a tabela existe**

Run: `python -c "from scripts.apply_schema import _create_engine; engine, connector = _create_engine(); print(engine.connect().exec_driver_sql(\"SELECT to_regclass('public.agenda_ur_evento')\").scalar()); connector.close()"`
Expected: imprime `agenda_ur_evento` (não `None`).

- [ ] **Step 4: Commit**

```bash
git add sql/schema/03-agenda-ur-evento.sql
git commit -m "feat: tabela agenda_ur_evento (historico de mutacao de UR, design doc §15 risco 12)"
```

---

### Task 2: `apps/agenda/repository.py`

**Files:**
- Create: `apps/agenda/repository.py`
- Test: `apps/agenda/tests/test_repository.py`

**Interfaces:**
- Consumes: `shared.cloudsql_client.get_db(financiador_id) -> CloudSQLClient` (Plano 03); a tabela `agenda_ur_evento` (Task 1 deste plano).
- Produces: `apps.agenda.repository.precedencia_origem(origem: str) -> int`; `apps.agenda.repository.upsert_agenda_ur(financiador_id: str, cabecalho: dict, pagamentos: list[dict]) -> dict` retornando `{"sobrescrito": bool, "agenda_ur": dict, "pagamentos": list[dict], "eventos": list[dict]}` (quando `sobrescrito` é `False`, só `"agenda_ur"` e `"eventos": []` vêm preenchidos — o lote foi descartado por ser mais antigo/menos precedente). Os Planos 06, 07 e 08 chamam exclusivamente esta função para persistir uma UR — nenhum deles deve fazer `get_db(...).table("agenda_ur")...` diretamente.

- [ ] **Step 1: Escrever `apps/agenda/repository.py`**

```python
"""Repositório de upsert de agenda_ur — regra de frescor e trilha de eventos
(design doc §8, §15 riscos 1, 7 e 12).

upsert_agenda_ur(financiador_id, cabecalho, pagamentos) é o único ponto de
escrita de uma UR, usado pelos Planos 06 (CERC síncrono), 07 (webhook) e 08
(arquivo AP005) — cada um traduz sua origem (SINCRONO/WEBHOOK/ARQUIVO) para
este mesmo formato e chama esta função, que decide se sobrescreve
(precedência WEBHOOK > SINCRONO > ARQUIVO, "mais recente sempre vence" —
§8), atualiza agenda_ur_pagamento removendo linhas de efeitos que não
vieram neste lote (§15 risco 7), e grava em agenda_ur_evento as transições
Captura/Bloqueio/Disponibilização/Liquidação que a timeline do front
("Eventos de Mutação" em ScheduleModal.tsx) precisa exibir (§15 risco 12).

`pagamentos` deve ser a lista COMPLETA e atual de efeitos da UR neste lote,
nunca um delta — essa premissa é o que permite decidir quais linhas antigas
ficaram obsoletas. `cabecalho["data_hora_ultima_atualizacao"]` deve ser um
datetime.datetime com tzinfo (RFC3339 já parseado pelo chamador).
"""
from datetime import datetime
from typing import Optional

from ulid import ULID

from shared.cloudsql_client import get_db

_CHAVE_UR = (
    "data_liquidacao",
    "entidade_registradora",
    "cnpj_credenciadora",
    "documento_ufr",
    "documento_titular",
    "codigo_arranjo",
)

_CHAVE_PAGAMENTO_EXTRA = ("tipo_informacao_pagamento", "indicador_efeitos_contrato")

_PRECEDENCIA_ORIGEM = {"WEBHOOK": 3, "SINCRONO": 2, "ARQUIVO": 1}


def precedencia_origem(origem: str) -> int:
    return _PRECEDENCIA_ORIGEM[origem]


def _com_filtros(query, valores: dict, campos):
    for campo in campos:
        query = query.eq(campo, valores[campo])
    return query


def _buscar_um(db, tabela: str, valores: dict, campos) -> Optional[dict]:
    resultado = _com_filtros(db.table(tabela).select("*"), valores, campos).execute()
    return resultado.data[0] if resultado.data else None


def _como_datetime(valor) -> datetime:
    # O driver (pg8000, via SQLAlchemy text()) pode devolver um
    # datetime.datetime nativo ou (dependendo da coluna/versão) uma string
    # RFC3339 — normaliza os dois casos antes de comparar, já que isso
    # nunca foi exercitado num teste desta base ainda.
    if isinstance(valor, datetime):
        return valor
    return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))


def _deve_sobrescrever(existente: Optional[dict], nova_data_hora: datetime, nova_origem: str) -> bool:
    if existente is None:
        return True
    atual = _como_datetime(existente["data_hora_ultima_atualizacao"])
    nova = _como_datetime(nova_data_hora)
    if nova > atual:
        return True
    if nova == atual:
        return precedencia_origem(nova_origem) > precedencia_origem(existente["origem"])
    return False


def _registrar_evento(db, chave: dict, tipo_evento: str, origem: str, valor, ocorrido_em: datetime) -> dict:
    evento = {campo: chave[campo] for campo in _CHAVE_UR}
    evento.update({
        "id": str(ULID()),
        "tipo_evento": tipo_evento,
        "origem": origem,
        "valor": valor,
        "ocorrido_em": ocorrido_em,
    })
    return db.table("agenda_ur_evento").insert(evento).execute().data[0]


def _upsert_cabecalho(db, chave: dict, cabecalho: dict, existente: Optional[dict], ocorrido_em: datetime) -> dict:
    dados = {**cabecalho, "atualizado_em": ocorrido_em}
    if existente is None:
        return db.table("agenda_ur").insert(dados).execute().data[0]
    return _com_filtros(db.table("agenda_ur").update(dados), chave, _CHAVE_UR).execute().data[0]


def _eventos_do_cabecalho(db, chave: dict, origem: str, existente: Optional[dict], novo: dict, ocorrido_em: datetime) -> list:
    if existente is None:
        return [_registrar_evento(db, chave, "CAPTURA", origem, novo["valor_total_ur"], ocorrido_em)]

    eventos = []
    if novo["valor_bloqueado"] > existente["valor_bloqueado"]:
        eventos.append(_registrar_evento(db, chave, "BLOQUEIO", origem, novo["valor_bloqueado"], ocorrido_em))
    if novo["valor_livre"] > existente["valor_livre"]:
        eventos.append(_registrar_evento(db, chave, "DISPONIBILIZACAO", origem, novo["valor_livre"], ocorrido_em))
    return eventos


def _upsert_pagamento(db, chave: dict, pagamento: dict, ocorrido_em: datetime):
    chave_pagamento = dict(chave)
    chave_pagamento["tipo_informacao_pagamento"] = pagamento["tipo_informacao_pagamento"]
    chave_pagamento["indicador_efeitos_contrato"] = pagamento.get("indicador_efeitos_contrato", "")

    campos_chave = _CHAVE_UR + _CHAVE_PAGAMENTO_EXTRA
    existente = _buscar_um(db, "agenda_ur_pagamento", chave_pagamento, campos_chave)

    dados = {**chave_pagamento, **pagamento, "atualizado_em": ocorrido_em}
    if existente is None:
        gravado = db.table("agenda_ur_pagamento").insert(dados).execute().data[0]
    else:
        gravado = _com_filtros(
            db.table("agenda_ur_pagamento").update(dados), chave_pagamento, campos_chave
        ).execute().data[0]

    liquidou_agora = (
        gravado.get("data_liquidacao_efetiva") is not None
        and (existente is None or existente.get("data_liquidacao_efetiva") is None)
    )
    return gravado, chave_pagamento, liquidou_agora


def _limpar_pagamentos_obsoletos(db, chave: dict, chaves_do_lote: set) -> None:
    existentes = _com_filtros(
        db.table("agenda_ur_pagamento").select("tipo_informacao_pagamento,indicador_efeitos_contrato"),
        chave, _CHAVE_UR,
    ).execute().data

    campos_chave = _CHAVE_UR + _CHAVE_PAGAMENTO_EXTRA
    for linha in existentes:
        chave_linha = (linha["tipo_informacao_pagamento"], linha["indicador_efeitos_contrato"])
        if chave_linha in chaves_do_lote:
            continue
        chave_pagamento = dict(chave)
        chave_pagamento["tipo_informacao_pagamento"] = linha["tipo_informacao_pagamento"]
        chave_pagamento["indicador_efeitos_contrato"] = linha["indicador_efeitos_contrato"]
        _com_filtros(db.table("agenda_ur_pagamento").delete(), chave_pagamento, campos_chave).execute()


def upsert_agenda_ur(financiador_id: str, cabecalho: dict, pagamentos: list) -> dict:
    db = get_db(financiador_id)
    chave = {campo: cabecalho[campo] for campo in _CHAVE_UR}
    origem = cabecalho["origem"]
    ocorrido_em = cabecalho["data_hora_ultima_atualizacao"]

    existente = _buscar_um(db, "agenda_ur", chave, _CHAVE_UR)
    if not _deve_sobrescrever(existente, ocorrido_em, origem):
        return {"sobrescrito": False, "agenda_ur": existente, "eventos": []}

    novo = _upsert_cabecalho(db, chave, cabecalho, existente, ocorrido_em)
    eventos = _eventos_do_cabecalho(db, chave, origem, existente, novo, ocorrido_em)

    chaves_do_lote = set()
    pagamentos_gravados = []
    for pagamento in pagamentos:
        gravado, chave_pagamento, liquidou_agora = _upsert_pagamento(db, chave, pagamento, ocorrido_em)
        chaves_do_lote.add((
            chave_pagamento["tipo_informacao_pagamento"],
            chave_pagamento["indicador_efeitos_contrato"],
        ))
        pagamentos_gravados.append(gravado)
        if liquidou_agora:
            valor = gravado.get("valor_liquidacao_efetiva")
            if valor is None:
                valor = gravado.get("valor_a_pagar")
            eventos.append(_registrar_evento(db, chave, "LIQUIDACAO", origem, valor, ocorrido_em))

    _limpar_pagamentos_obsoletos(db, chave, chaves_do_lote)

    return {"sobrescrito": True, "agenda_ur": novo, "pagamentos": pagamentos_gravados, "eventos": eventos}
```

- [ ] **Step 2: Escrever `apps/agenda/tests/test_repository.py`**

```python
from datetime import datetime, timezone

import pytest

from shared.cloudsql_client import get_db
from apps.agenda.repository import (
    _CHAVE_UR,
    _com_filtros,
    precedencia_origem,
    upsert_agenda_ur,
)

FINANCIADOR_TESTE = "12345678000199"

CHAVE_TESTE = {
    "data_liquidacao": "2026-09-15",
    "entidade_registradora": "22246686000196",
    "cnpj_credenciadora": "01027058000191",
    "documento_ufr": "12345678000199",
    "documento_titular": "12345678000199",
    "codigo_arranjo": "VCC",
}


def _apagar_ur_teste():
    db = get_db(FINANCIADOR_TESTE)
    _com_filtros(db.table("agenda_ur_evento").delete(), CHAVE_TESTE, _CHAVE_UR).execute()
    _com_filtros(db.table("agenda_ur_pagamento").delete(), CHAVE_TESTE, _CHAVE_UR).execute()
    _com_filtros(db.table("agenda_ur").delete(), CHAVE_TESTE, _CHAVE_UR).execute()


@pytest.fixture(autouse=True)
def _limpar_ur_teste():
    _apagar_ur_teste()
    yield
    _apagar_ur_teste()


def _cabecalho(data_hora, origem, **overrides):
    base = {
        **CHAVE_TESTE,
        "constituicao": "1",
        "valor_constituido_total": 1000,
        "valor_constituido_antecipacao_pre": 0,
        "valor_bloqueado": 0,
        "valor_livre": 0,
        "valor_total_ur": 1000,
        "carteira": None,
        "data_hora_ultima_atualizacao": data_hora,
        "origem": origem,
        "origem_arquivo": None,
    }
    base.update(overrides)
    return base


def _pagamento(tipo, **overrides):
    base = {
        "tipo_informacao_pagamento": tipo,
        "indicador_efeitos_contrato": "",
        "identificador_cerc_contrato": None,
        "regras_divisao": None,
        "valor_onerado": None,
        "valor_constituido_efeito": None,
        "valor_a_pagar": 1000,
        "beneficiario": None,
        "data_liquidacao_efetiva": None,
        "valor_liquidacao_efetiva": None,
        "motivo_nao_pagamento": None,
        "domicilio": {},
    }
    base.update(overrides)
    return base


def test_precedencia_origem_ordem_esperada():
    assert precedencia_origem("WEBHOOK") > precedencia_origem("SINCRONO")
    assert precedencia_origem("SINCRONO") > precedencia_origem("ARQUIVO")


def test_upsert_cria_ur_pela_primeira_vez_e_gera_evento_captura():
    data_hora = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    resultado = upsert_agenda_ur(
        FINANCIADOR_TESTE,
        _cabecalho(data_hora, "SINCRONO", valor_total_ur=1500, valor_constituido_total=1500),
        pagamentos=[],
    )

    assert resultado["sobrescrito"] is True
    assert resultado["agenda_ur"]["valor_total_ur"] == 1500
    assert len(resultado["eventos"]) == 1
    assert resultado["eventos"][0]["tipo_evento"] == "CAPTURA"


def test_upsert_nao_sobrescreve_quando_lote_mais_antigo():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t2, "SINCRONO", valor_total_ur=1000), pagamentos=[])

    resultado = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "ARQUIVO", valor_total_ur=1), pagamentos=[])

    assert resultado["sobrescrito"] is False
    assert resultado["agenda_ur"]["valor_total_ur"] == 1000


def test_upsert_sobrescreve_quando_mais_recente():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "ARQUIVO", valor_total_ur=1000), pagamentos=[])

    resultado = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t2, "SINCRONO", valor_total_ur=2000), pagamentos=[])

    assert resultado["sobrescrito"] is True
    assert resultado["agenda_ur"]["valor_total_ur"] == 2000


def test_upsert_empate_de_timestamp_resolve_por_precedencia():
    t = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t, "ARQUIVO", valor_total_ur=1000), pagamentos=[])

    ganha = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t, "SINCRONO", valor_total_ur=2000), pagamentos=[])
    assert ganha["sobrescrito"] is True
    assert ganha["agenda_ur"]["valor_total_ur"] == 2000

    perde = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t, "ARQUIVO", valor_total_ur=3000), pagamentos=[])
    assert perde["sobrescrito"] is False
    assert perde["agenda_ur"]["valor_total_ur"] == 2000


def test_upsert_gera_evento_bloqueio_quando_valor_bloqueado_aumenta():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    upsert_agenda_ur(
        FINANCIADOR_TESTE,
        _cabecalho(t1, "SINCRONO", valor_bloqueado=0, valor_livre=1000),
        pagamentos=[],
    )

    resultado = upsert_agenda_ur(
        FINANCIADOR_TESTE,
        _cabecalho(t2, "WEBHOOK", valor_bloqueado=400, valor_livre=600),
        pagamentos=[],
    )

    tipos = {e["tipo_evento"] for e in resultado["eventos"]}
    assert tipos == {"BLOQUEIO"}


def test_upsert_gera_evento_disponibilizacao_quando_valor_livre_aumenta():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    upsert_agenda_ur(
        FINANCIADOR_TESTE,
        _cabecalho(t1, "SINCRONO", valor_bloqueado=400, valor_livre=0),
        pagamentos=[],
    )

    resultado = upsert_agenda_ur(
        FINANCIADOR_TESTE,
        _cabecalho(t2, "WEBHOOK", valor_bloqueado=0, valor_livre=400),
        pagamentos=[],
    )

    tipos = {e["tipo_evento"] for e in resultado["eventos"]}
    assert tipos == {"DISPONIBILIZACAO"}


def test_upsert_gera_evento_liquidacao_quando_pagamento_recebe_data_liquidacao_efetiva():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    pendente = _pagamento("5")
    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "SINCRONO"), pagamentos=[pendente])

    liquidado = _pagamento("5", data_liquidacao_efetiva="2026-09-15", valor_liquidacao_efetiva=1000)
    resultado = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t2, "WEBHOOK"), pagamentos=[liquidado])

    eventos_liquidacao = [e for e in resultado["eventos"] if e["tipo_evento"] == "LIQUIDACAO"]
    assert len(eventos_liquidacao) == 1
    assert eventos_liquidacao[0]["valor"] == 1000


def test_upsert_remove_pagamento_obsoleto_fora_do_lote_atual():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    p1 = _pagamento("1")
    p2 = _pagamento("2")
    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "SINCRONO"), pagamentos=[p1, p2])

    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t2, "WEBHOOK"), pagamentos=[p1])

    restantes = _com_filtros(
        get_db(FINANCIADOR_TESTE).table("agenda_ur_pagamento").select("tipo_informacao_pagamento"),
        CHAVE_TESTE, _CHAVE_UR,
    ).execute().data
    tipos_restantes = {r["tipo_informacao_pagamento"] for r in restantes}
    assert tipos_restantes == {"1"}
```

- [ ] **Step 3: Rodar a suíte**

Run: `pytest apps/agenda/tests/test_repository.py -v`
Expected: PASS em todos os 9 testes (hitam o banco `agenda` real de dev via `TENANT_12345678000199_CONFIG` do `.env`, mesma estratégia de `shared/tests/test_cloudsql_client.py` — sem mocking do banco).

- [ ] **Step 4: Commit**

```bash
git add apps/agenda/repository.py apps/agenda/tests/test_repository.py
git commit -m "feat: repositorio de upsert de agenda_ur com regra de frescor e trilha de eventos"
```

---

## Self-Review Notes

- **Spec coverage:** design doc §8 (regra de frescor `WEBHOOK > SINCRONO > ARQUIVO`, "mais recente sempre vence") → `_deve_sobrescrever`/`precedencia_origem`, testado em 3 cenários (mais antigo, mais recente, empate). §15 risco 7 (linhas obsoletas de `agenda_ur_pagamento`) → `_limpar_pagamentos_obsoletos`, testado. §15 risco 12 (histórico de eventos) → `agenda_ur_evento` + `_eventos_do_cabecalho`/`_upsert_pagamento`, testado para os 4 tipos (`CAPTURA`, `BLOQUEIO`, `DISPONIBILIZACAO`, `LIQUIDACAO`). §15 risco 1 → decisão explícita por opção (b), documentada em Global Constraints, `shared/cloudsql_client.py` não tocado.
- **Placeholder scan:** nenhum — todo step tem código executável completo.
- **Type consistency:** `upsert_agenda_ur(financiador_id: str, cabecalho: dict, pagamentos: list[dict]) -> dict` é a assinatura exata que os Planos 06/07/08 vão chamar; `precedencia_origem(origem: str) -> int` é a função citada verbatim no design doc §8. Nomes de coluna em `cabecalho`/`pagamento` batem exatamente com `sql/schema/01-agenda-schema.sql` (`agenda_ur`, `agenda_ur_pagamento`) e com a nova `agenda_ur_evento` (Task 1).
- **Risco aceito e não resolvido por este plano:** `_como_datetime` normaliza o valor de `data_hora_ultima_atualizacao` vindo do banco caso o driver devolva string em vez de `datetime` nativo — essa ambiguidade nunca foi exercitada em teste nesta base (os testes existentes de `shared/tests/test_cloudsql_client.py` não comparam valores de coluna `TIMESTAMPTZ` recuperados do banco). Se o Step 3 da Task 2 falhar por erro de comparação de tipo, é esse o primeiro lugar a olhar — não uma reprovação do plano.
- **Fora de escopo deste plano (registrado no design doc §15, riscos 10 e 11):** nome amigável de credenciadora e seed/estrutura de `dominio_arranjo` — não usados por `repository.py`, tratados nos planos que expõem/decoram esses dados (06/09).

**Next:** `2026-08-24-agenda-plan-06-cerc-client.md` (cliente CERC de consulta — batch e online, validações A01–A10, política de consulta) — ainda não escrito.
