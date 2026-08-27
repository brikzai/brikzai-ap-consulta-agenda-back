# agenda-service — Plan 09: API Interna — Ciclo de Vida da Consulta — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Nota de série:** o design doc §16 chama o próximo passo de "Plano 09 — API interna completa (endpoints restantes de §10) + compliance", um único item de ~6 endpoints. Pesquisa feita antes deste plano (ver Global Constraints) confirmou que nenhum dos dois serviços irmãos (`ap-back-contratos`, `ap-back-optin`) tem paginação por cursor, agregação (`SUM`/`GROUP BY`) ou relatório de compliance já construídos — esses três precisam de desenho próprio, sem precedente pra copiar. Para manter a granularidade de "planos pequenos, cada um independentemente revisável" (design doc §16), este plano cobre só o **ciclo de vida da consulta** (`POST`/`GET /agendas/consultas`, `config/politicas-consulta`) — os três endpoints de leitura/relatório (`GET /agendas/urs`, `GET /agendas/urs/posicao`, `GET /compliance/relatorio`) ficam para o Plano 10, e o antigo "Plano 10 — observabilidade" desliza para Plano 11.

**Goal:** Expor `POST /api/v1/agendas/consultas` (dispara consulta batch/online, JWT-protegido, primeira vez que `shared/jwt_auth.jwt_required` é usado num endpoint real), `GET /api/v1/agendas/consultas/{id}` (status consolidado, contagem de URs por origem, frescor), e `GET/PUT/DELETE /api/v1/config/politicas-consulta` (CRUD self-service da política de consulta) — fechando também o design doc §15 risco 14 (vínculo síncrono nunca gravado em `consulta_agenda_ur`), que bloqueava a contagem por origem do segundo endpoint.

**Architecture:** Três tasks, cada uma um endpoint (par de endpoints no caso da Task 2, que abrange leitura + o fix no caminho de escrita síncrona de que ela depende). Todas usam `@jwt_required` (Plano 04) pela primeira vez num endpoint real — `request.financiador_id` e `request.jwt_claims` vêm de lá, nunca da URL/corpo. Task 1 é um wrapper fino em cima de `services.cerc.client.consultar_agenda` (Plano 06, já testado). Task 2 modifica `consultar_agenda` (Plano 06) para gravar em `consulta_agenda_ur` com `origem='SINCRONO'` — mesma tabela que o webhook (Plano 07) já usa — e depois lê essa tabela como fonte única de verdade pra contagem por origem. Task 3 é CRUD simples sobre `politica_consulta` (schema do Plano 02, já usada por `apps/agenda/validation.py` desde o Plano 06).

**Tech Stack:** Nenhuma dependência nova.

**Spec:** `docs/superpowers/specs/2026-08-24-agenda-service-design.md` §7 (política de consulta), §8 (integração CERC, regra de upsert), §10 (tabela de endpoints), §11 (compliance), §15 riscos 1 e 14. `docs/specs/SPEC-03-consulta-de-agenda-ap005.md` §7.1/§7.2 (contratos de request/response dos dois primeiros endpoints), §8 (requisitos de compliance da consulta online). Referência de implementação: `services/cerc/client.py` (`consultar_agenda`, Plano 06) e `apps/agenda/validation.py` (Plano 06) — ambos reutilizados, não recriados. Série: plano 9 de ~11 (ver Nota de série).

**Depends on:** `2026-08-24-agenda-plan-04-auth.md` (`shared/jwt_auth.jwt_required`), `2026-08-25-agenda-plan-06-cerc-client.md` (`services/cerc/client.consultar_agenda`, `apps/agenda/validation.py`), `2026-08-25-agenda-plan-05-upsert-repository.md` (`apps/agenda/repository._CHAVE_UR`, `._buscar_um`, `._com_filtros`).

## Global Constraints

- **Convenção de JSON na borda HTTP: camelCase.** Confirmado nos dois serviços irmãos (`ap-back-optin/optin/apps/optin/views.py`, função `_serializar_optin`) — toda resposta serializa colunas `snake_case` do banco para chaves `camelCase`, via uma função `_serializar_*` dedicada por recurso. Todo request/response deste plano segue essa convenção; internamente (dicts passados pra `consultar_agenda`/`repository`) continua tudo `snake_case`, sem mudar contrato de módulos já existentes.
- **Forma do erro JSON, uniforme nos três endpoints:** `{"erro": "<CODIGO>", "mensagem": "<texto>"}` — mesmo formato que `shared/jwt_auth.py` já usa pro 401 (`{"erro": "NAO_AUTENTICADO", ...}`). `<CODIGO>` é sempre uma string em maiúsculas com underscore, nunca um número solto.
- **`ValidacaoConsultaError` → status HTTP, mapeamento binário (não uma tabela ad-hoc):** códigos que representam bloqueio de autorização/regra de negócio (fail-closed, "nem chama a CERC") viram `403`: `SEM_BASE_AUTORIZATIVA`, `POLITICA_NAO_CONFIGURADA`, `MODO_NAO_PERMITIDO`, `RATE_LIMIT_EXCEDIDO`, `CARTEIRA_OBRIGATORIA`. Todo o resto (os códigos `105xxx` de `A01`/`A02`/`A03`/`A05`, mais `TIPO_AVALIACAO_INVALIDO`/`MISTURA_CURINGA_INVALIDA`) vira `422` — mesma equivalência que `services/cerc/client.py` já declara no docstring de `CercConsultaInvalidaError` ("equivalente a 422 local"), não uma convenção nova.
- **`CercConsultaError` → status HTTP:** `CercConsultaCriticaError` → `502` (falha grave do lado da CERC, alerta); `CercConsultaRetentavelError` → `503` (retentável); `CercConsultaInvalidaError` → `422`. Qualquer outra exceção não mapeada → `500`, logada com `logger.exception`.
- **`ator` e `origem_ip` (design doc §11, compliance):** `ator = request.jwt_claims.get("sub") or "desconhecido"`; `origem_ip = request.META.get("REMOTE_ADDR")`. Nenhum dos dois vem do corpo da requisição — o corpo nunca pode fingir ser outro ator.
- **Resolução do design doc §15 risco 14 (Task 2):** `consultar_agenda` (Plano 06) passa a gravar uma linha em `consulta_agenda_ur` com `origem='SINCRONO'` para cada UR que persiste — mesma tabela que o webhook (Plano 07) já usa com `origem='WEBHOOK'`. Isso fecha o gap que o risco 14 deixou aberto ("`consulta_agenda_ur` não é escrito para URs síncronas") exatamente quando um consumidor real (`GET /agendas/consultas/{id}`) precisa do dado. A partir desta task, `consulta_agenda_ur` agrupado por `origem` é a **única fonte de verdade** para "contagem de URs por origem" exposta por esse endpoint — a coluna `qtd_urs_sincrono` continua existindo e sendo escrita (não removida, não é este plano que decide isso), mas não é mais lida por este endpoint. `ARQUIVO` permanece **sempre `0`** nessa contagem — a ingestão de arquivo (Plano 08) nunca associa uma UR a nenhuma `consulta_agenda` (não existe consulta nenhuma no fluxo de arquivo) — isso é uma característica do desenho, documentada aqui, não um bug a corrigir.
- **`politica_consulta` DELETE é sempre soft-delete (`ativo=false`), nunca remoção física da linha.** Mesma filosofia de retenção/trilha de auditoria já estabelecida no design doc (§11, "retenção de 5 anos, sem expurgo") — mesmo essa tabela não sendo, ela mesma, sujeita a essa regra especificamente, remover fisicamente uma política jogaria fora o histórico de quando/quem mudou a regra de acesso a um `motivo`, sem necessidade.
- **Sem paginação neste plano.** `GET /api/v1/agendas/consultas/{id}` devolve um recurso único; `GET/PUT/DELETE /api/v1/config/politicas-consulta` lista uma tabela pequena (uma linha por `motivo`, tipicamente poucas linhas por tenant) sem cursor. Paginação por cursor é desenho novo (confirmado: nenhum precedente nos irmãos) e fica isolada no Plano 10, junto dos dois endpoints que realmente precisam dela.
- **Sem alterações em `shared/cloudsql_client.py`** — nenhuma das três tasks precisa de agregação (`SUM`/`GROUP BY`) ou operador novo; tudo é `select`/`insert`/`update` simples já suportado.
- **CSRF:** não há `CsrfViewMiddleware` no `MIDDLEWARE` deste projeto (`config/settings.py`) — nenhuma view precisa de `@csrf_exempt`, incluindo as que aceitam `PUT`/`DELETE` (Task 3). Mesma configuração que já vale pros webhooks (Plano 07).

---

### Task 1: `POST /api/v1/agendas/consultas`

**Files:**
- Modify: `apps/agenda/views.py` (novos imports; nova view `criar_consulta_agenda`)
- Modify: `apps/agenda/urls.py` (nova rota)
- Test: `apps/agenda/tests/test_views_criar_consulta.py`

**Interfaces:**
- Consumes: `services.cerc.client.consultar_agenda(financiador_id, consulta) -> dict` (Plano 06, já existe — `consulta` é um dict `snake_case` com chaves `modo, documento_ufr, documento_titular, credenciadoras, arranjos, data_inicio, data_fim, tipo_avaliacao, participante, carteira, base_autorizativa, motivo, ator, origem_ip`); `apps.agenda.validation.ValidacaoConsultaError` (`.codigo`, `.mensagem`); `services.cerc.client.{CercConsultaCriticaError, CercConsultaRetentavelError, CercConsultaInvalidaError}`; `shared.jwt_auth.jwt_required` (popula `request.financiador_id`, `request.jwt_claims`).
- Produces: `POST /api/v1/agendas/consultas` — `401` sem JWT válido; `400` corpo não-JSON, campo obrigatório ausente, ou `modo` fora de `ONLINE`/`BATCH`; `403`/`422` conforme `ValidacaoConsultaError` (ver Global Constraints); `502`/`503`/`422` conforme erro da CERC; `500` em falha inesperada; sucesso: `200` (`BATCH`) ou `202` (`ONLINE`) com `{"consultaId", "status", "agendas"}`.

- [ ] **Step 1: Escrever `apps/agenda/tests/test_views_criar_consulta.py`**

```python
import json
import time

import httpx
import jwt as pyjwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import Client
from ulid import ULID

from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
UFR_TESTE = "11222333000181"
URL = "/api/v1/agendas/consultas"
URL_CERC = "https://ap-homolog.cerc.inf.br/v15/agenda/consultar"


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


def _token(private_pem, **overrides):
    payload = {
        "exp": int(time.time()) + 300, "iss": "brikz-iam", "sub": "analista@teste.com",
        "financiador_id": FINANCIADOR_TESTE,
    }
    payload.update(overrides)
    return pyjwt.encode(payload, private_pem, algorithm="RS256")


def _limpar():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").delete().eq("filtro_ufr", UFR_TESTE).execute()
    db.table("cerc_requisicao").delete().eq("recurso", "agenda_consultar").execute()
    db.table("politica_consulta").delete().eq("motivo", "TESTE-VIEW-CONSULTAS").execute()


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch, keypair):
    _, public_pem = keypair
    monkeypatch.setenv("IAM_JWT_PUBLIC_KEY", public_pem)
    monkeypatch.setenv("IAM_JWT_ISSUER", "brikz-iam")
    monkeypatch.setenv("CERC_API_BASE_URL", "https://ap-homolog.cerc.inf.br")

    from services.cerc import client as cerc_client
    monkeypatch.setattr(cerc_client, "get_cerc_token", lambda financiador_id: "token-teste")
    monkeypatch.setattr(cerc_client, "invalidate_token", lambda financiador_id: None)

    _limpar()
    db = get_db(FINANCIADOR_TESTE)
    db.table("politica_consulta").insert({
        "id": str(ULID()), "motivo": "TESTE-VIEW-CONSULTAS",
        "modos_permitidos": ["BATCH", "ONLINE"], "ativo": True,
    }).execute()
    yield
    _limpar()


def _corpo_consulta(**overrides):
    base = {
        "modo": "BATCH",
        "usuarioFinalRecebedor": UFR_TESTE,
        "credenciadoras": ["99T"],
        "arranjos": ["99T"],
        "dataInicio": "2026-09-01",
        "dataFim": "2026-09-30",
        "baseAutorizativa": {"tipo": "OPTIN", "id": "opt_1"},
        "motivo": "TESTE-VIEW-CONSULTAS",
    }
    base.update(overrides)
    return base


def test_sem_jwt_retorna_401():
    response = Client().post(URL, data=json.dumps(_corpo_consulta()), content_type="application/json")
    assert response.status_code == 401


def test_corpo_nao_json_retorna_400(keypair):
    private_pem, _ = keypair
    response = Client().post(
        URL, data="isto nao e json", content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 400
    assert response.json()["erro"] == "CORPO_INVALIDO"


def test_campo_obrigatorio_ausente_retorna_400(keypair):
    private_pem, _ = keypair
    corpo = _corpo_consulta()
    del corpo["motivo"]
    response = Client().post(
        URL, data=json.dumps(corpo), content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 400
    assert response.json()["erro"] == "CAMPO_OBRIGATORIO_AUSENTE"


def test_modo_invalido_retorna_400(keypair):
    private_pem, _ = keypair
    response = Client().post(
        URL, data=json.dumps(_corpo_consulta(modo="ESTRANHO")), content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 400
    assert response.json()["erro"] == "MODO_INVALIDO"


def test_politica_nao_configurada_retorna_403(keypair):
    private_pem, _ = keypair
    response = Client().post(
        URL, data=json.dumps(_corpo_consulta(motivo="MOTIVO-SEM-POLITICA")), content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 403
    assert response.json()["erro"] == "POLITICA_NAO_CONFIGURADA"


@respx.mock
def test_batch_sucesso_retorna_200_e_persiste(keypair):
    private_pem, _ = keypair
    respx.post(URL_CERC).mock(return_value=httpx.Response(200, json=[]))

    response = Client().post(
        URL, data=json.dumps(_corpo_consulta()), content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 200
    corpo = response.json()
    assert corpo["status"] == "COMPLETA"
    assert corpo["agendas"] == []

    db = get_db(FINANCIADOR_TESTE)
    consulta = db.table("consulta_agenda").select("*").eq("id", corpo["consultaId"]).execute().data[0]
    assert consulta["modo"] == "BATCH"
    assert consulta["ator"] == "analista@teste.com"


@respx.mock
def test_online_sucesso_retorna_202(keypair):
    private_pem, _ = keypair
    respx.post(URL_CERC).mock(return_value=httpx.Response(200, json=[]))

    response = Client().post(
        URL, data=json.dumps(_corpo_consulta(modo="ONLINE")), content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 202
    assert response.json()["status"] == "PARCIAL"


@respx.mock
def test_erro_critico_cerc_retorna_502(keypair):
    private_pem, _ = keypair
    respx.post(URL_CERC).mock(return_value=httpx.Response(401))

    response = Client().post(
        URL, data=json.dumps(_corpo_consulta()), content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 502
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `pytest apps/agenda/tests/test_views_criar_consulta.py -v`
Expected: FAIL — rota `agendas/consultas` não existe (404) ou `AttributeError` (view não existe)

- [ ] **Step 3: Adicionar a view em `apps/agenda/views.py`**

No topo do arquivo, ajustar o import de `datetime` (linha existente `from datetime import datetime, timezone`) para incluir `date`:

```python
from datetime import date, datetime, timezone
```

Adicionar aos imports (junto aos demais `apps.agenda`/`services`/`shared`):

```python
from apps.agenda.validation import ValidacaoConsultaError
from services.cerc.client import (
    CercConsultaCriticaError,
    CercConsultaInvalidaError,
    CercConsultaRetentavelError,
    consultar_agenda,
)
from shared.jwt_auth import jwt_required
```

No final do arquivo:

```python
_CODIGOS_VALIDACAO_403 = {
    "SEM_BASE_AUTORIZATIVA", "POLITICA_NAO_CONFIGURADA", "MODO_NAO_PERMITIDO",
    "RATE_LIMIT_EXCEDIDO", "CARTEIRA_OBRIGATORIA",
}


def _status_para_validacao(codigo: str) -> int:
    return 403 if codigo in _CODIGOS_VALIDACAO_403 else 422


def _parse_data_iso(valor, nome: str) -> date:
    if not valor:
        raise ValueError(f"{nome} é obrigatório")
    try:
        return date.fromisoformat(valor)
    except ValueError:
        raise ValueError(f"{nome} deve estar no formato AAAA-MM-DD: {valor!r}")


def _traduzir_requisicao_consulta(payload: dict, request) -> dict:
    base_autorizativa = payload.get("baseAutorizativa") or {}
    return {
        "modo": payload.get("modo"),
        "documento_ufr": payload.get("usuarioFinalRecebedor"),
        "documento_titular": payload.get("titular"),
        "credenciadoras": payload.get("credenciadoras") or [],
        "arranjos": payload.get("arranjos") or [],
        "data_inicio": _parse_data_iso(payload.get("dataInicio"), "dataInicio"),
        "data_fim": _parse_data_iso(payload.get("dataFim"), "dataFim"),
        "tipo_avaliacao": payload.get("tipoAvaliacao"),
        "participante": payload.get("participante"),
        "carteira": payload.get("carteira"),
        "base_autorizativa": {"tipo": base_autorizativa.get("tipo"), "id": base_autorizativa.get("id")},
        "motivo": payload.get("motivo"),
        "ator": request.jwt_claims.get("sub") or "desconhecido",
        "origem_ip": request.META.get("REMOTE_ADDR"),
    }


_CAMPOS_OBRIGATORIOS_CONSULTA = (
    "modo", "usuarioFinalRecebedor", "credenciadoras", "arranjos",
    "dataInicio", "dataFim", "baseAutorizativa", "motivo",
)


@jwt_required
@require_POST
def criar_consulta_agenda(request):
    """POST /api/v1/agendas/consultas (design doc §10, SPEC03 §7.1) —
    wrapper fino em cima de services.cerc.client.consultar_agenda (Plano
    06), que já roda A01-A10 e faz a chamada à CERC. Esta view só traduz
    o payload camelCase pro dict snake_case que consultar_agenda espera,
    mapeia exceções pra status HTTP (Global Constraints deste plano), e
    preenche ator/origem_ip a partir do JWT — nunca do corpo."""
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"erro": "CORPO_INVALIDO", "mensagem": "corpo não é JSON válido"}, status=400)

    if not isinstance(payload, dict):
        return JsonResponse({"erro": "CORPO_INVALIDO", "mensagem": "corpo deve ser um objeto JSON"}, status=400)

    faltando = [campo for campo in _CAMPOS_OBRIGATORIOS_CONSULTA if not payload.get(campo)]
    if faltando:
        return JsonResponse(
            {"erro": "CAMPO_OBRIGATORIO_AUSENTE", "mensagem": f"campos obrigatórios ausentes: {', '.join(faltando)}"},
            status=400,
        )

    if payload["modo"] not in ("ONLINE", "BATCH"):
        return JsonResponse({"erro": "MODO_INVALIDO", "mensagem": "modo deve ser ONLINE ou BATCH"}, status=400)

    try:
        consulta = _traduzir_requisicao_consulta(payload, request)
    except ValueError as exc:
        return JsonResponse({"erro": "DATA_INVALIDA", "mensagem": str(exc)}, status=400)

    try:
        resultado = consultar_agenda(request.financiador_id, consulta)
    except ValidacaoConsultaError as exc:
        return JsonResponse({"erro": exc.codigo, "mensagem": exc.mensagem}, status=_status_para_validacao(exc.codigo))
    except CercConsultaCriticaError as exc:
        logger.error("[Consultas] Erro crítico da CERC (financiador=%s): %s", request.financiador_id, exc)
        return JsonResponse({"erro": exc.codigo, "mensagem": exc.mensagem}, status=502)
    except CercConsultaRetentavelError as exc:
        return JsonResponse({"erro": exc.codigo, "mensagem": exc.mensagem}, status=503)
    except CercConsultaInvalidaError as exc:
        return JsonResponse({"erro": exc.codigo, "mensagem": exc.mensagem}, status=422)
    except Exception:
        logger.exception("[Consultas] Falha inesperada ao criar consulta (financiador=%s)", request.financiador_id)
        return JsonResponse({"erro": "ERRO_INTERNO", "mensagem": "falha inesperada ao processar a consulta"}, status=500)

    status_http = 200 if consulta["modo"] == "BATCH" else 202
    return JsonResponse(
        {"consultaId": resultado["consultaId"], "status": resultado["status"], "agendas": resultado["agendas"]},
        status=status_http,
    )
```

- [ ] **Step 4: Registrar a rota em `apps/agenda/urls.py`**

```python
from django.urls import path, re_path
from . import views

urlpatterns = [
    path("health", views.health),
    re_path(r"^webhooks/agenda/(?P<financiador_id>\d{14})$", views.webhook_agenda),
    path("webhooks/agenda/processar", views.processar_webhook_agenda),
    path("jobs/varrer-completude", views.varrer_completude),
    re_path(r"^jobs/importar-ap005/(?P<financiador_id>\d{14})$", views.importar_ap005),
    path("agendas/consultas", views.criar_consulta_agenda),
]
```

- [ ] **Step 5: Rodar os testes e confirmar que passam** (contra o banco `agenda` real de dev)

Run: `pytest apps/agenda/tests/test_views_criar_consulta.py -v`
Expected: PASS (7 testes)

- [ ] **Step 6: Commit**

```bash
git add apps/agenda/views.py apps/agenda/urls.py apps/agenda/tests/test_views_criar_consulta.py
git commit -m "feat: endpoint POST /api/v1/agendas/consultas (Plano 09, Task 1)"
```

---

### Task 2: `GET /api/v1/agendas/consultas/{id}` + fecha o risco 14 (vínculo síncrono)

**Files:**
- Modify: `services/cerc/client.py` (grava em `consulta_agenda_ur` com `origem='SINCRONO'`)
- Modify: `services/cerc/tests/test_client.py` (nova asserção + limpeza da tabela nova)
- Modify: `apps/agenda/views.py` (novos imports; nova view `obter_consulta_agenda`)
- Modify: `apps/agenda/urls.py` (nova rota)
- Test: `apps/agenda/tests/test_views_obter_consulta.py`

**Interfaces:**
- Consumes: `apps.agenda.repository._CHAVE_UR`, `._buscar_um(db, tabela, valores, campos)` (Plano 05, já existem, já usados fora de `repository.py` em testes — reuso cruzado precedente).
- Produces: `GET /api/v1/agendas/consultas/{consultaId}` — `401` sem JWT; `404` consulta não encontrada; sucesso `200` com `{"consultaId", "modo", "status", "filtroUfr", "filtroTitular", "filtroCredenciadoras", "filtroArranjos", "filtroDataInicio", "filtroDataFim", "contagemPorOrigem": {"SINCRONO": N, "WEBHOOK": N, "ARQUIVO": 0}, "frescor": {"maisAntigo": "...", "maisRecente": "..."} | null, "iniciadaEm", "encerradaEm"}`.

- [ ] **Step 1: Escrever a nova asserção em `services/cerc/tests/test_client.py`**

Adicionar ao final do arquivo (mesmo padrão dos testes existentes: usa `_consulta_base()`, `_resposta_cerc()`, `@respx.mock`):

```python
@respx.mock
def test_consultar_agenda_vincula_ur_em_consulta_agenda_ur_com_origem_sincrono():
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc()))

    resultado = client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    db = get_db(FINANCIADOR_TESTE)
    vinculos = db.table("consulta_agenda_ur").select("*").eq("consulta_id", resultado["consultaId"]).execute().data
    assert len(vinculos) == 1
    assert vinculos[0]["origem"] == "SINCRONO"
    assert vinculos[0]["entidade_registradora"] == "22246686000196"
    assert vinculos[0]["documento_titular"] == CNPJ_VALIDO
```

Também ajustar `_limpar()` (já existe no topo do arquivo) para também apagar as linhas novas — adicionar esta linha dentro da função, junto às demais deleções por `data_liquidacao`/`entidade_registradora`:

```python
def _limpar():
    db = get_db(FINANCIADOR_TESTE)
    parcial = {"data_liquidacao": CHAVE_UR_TESTE["data_liquidacao"], "entidade_registradora": CHAVE_UR_TESTE["entidade_registradora"]}
    campos_parciais = ("data_liquidacao", "entidade_registradora")
    repository._com_filtros(db.table("consulta_agenda_ur").delete(), parcial, campos_parciais).execute()  # NOVA LINHA
    repository._com_filtros(db.table("agenda_ur_evento").delete(), parcial, campos_parciais).execute()
    repository._com_filtros(db.table("agenda_ur_pagamento").delete(), parcial, campos_parciais).execute()
    repository._com_filtros(db.table("agenda_ur").delete(), parcial, campos_parciais).execute()
    db.table("consulta_agenda").delete().eq("filtro_ufr", CNPJ_VALIDO).execute()
    db.table("cerc_requisicao").delete().eq("recurso", "agenda_consultar").execute()
    db.table("politica_consulta").delete().eq("motivo", "TESTE-CLIENT").execute()
```

- [ ] **Step 2: Rodar o teste novo e confirmar que falha**

Run: `pytest services/cerc/tests/test_client.py::test_consultar_agenda_vincula_ur_em_consulta_agenda_ur_com_origem_sincrono -v`
Expected: FAIL — `assert len(vinculos) == 1` falha com `0 == 1` (a tabela ainda não é escrita pelo caminho síncrono)

- [ ] **Step 3: Modificar `services/cerc/client.py`**

Adicionar, próximo às outras funções auxiliares de escrita (`_registrar_ur_rejeitada`, `_criar_consulta_agenda`):

```python
def _vincular_consulta_ur(financiador_id: str, consulta_id: str, cabecalho: dict) -> None:
    get_db(financiador_id).table("consulta_agenda_ur").insert({
        "consulta_id": consulta_id,
        "entidade_registradora": cabecalho["entidade_registradora"],
        "cnpj_credenciadora": cabecalho["cnpj_credenciadora"],
        "documento_ufr": cabecalho["documento_ufr"],
        "documento_titular": cabecalho["documento_titular"],
        "codigo_arranjo": cabecalho["codigo_arranjo"],
        "data_liquidacao": cabecalho["data_liquidacao"],
        "origem": "SINCRONO",
    }).execute()
```

No corpo de `consultar_agenda`, dentro do loop que já chama `upsert_agenda_ur`, adicionar a chamada logo em seguida (a função inteira, com a única linha nova marcada):

```python
    qtd_urs = 0
    for agenda in agendas:
        for ur in agenda.get("unidadesRecebiveis", []):
            try:
                linhas = _traduzir_ur(agenda, ur)
                for cabecalho, pagamentos in linhas:
                    upsert_agenda_ur(financiador_id, cabecalho, pagamentos)
                    _vincular_consulta_ur(financiador_id, consulta_id, cabecalho)  # NOVA LINHA — fecha risco 14
                    qtd_urs += 1
            except Exception as exc:
                _registrar_ur_rejeitada(financiador_id, ur, exc)
```

- [ ] **Step 4: Rodar todos os testes de `test_client.py` e confirmar que passam**

Run: `pytest services/cerc/tests/test_client.py -v`
Expected: PASS (todos, incluindo o teste novo)

- [ ] **Step 5: Commit do fix**

```bash
git add services/cerc/client.py services/cerc/tests/test_client.py
git commit -m "fix: consultar_agenda vincula UR em consulta_agenda_ur (origem SINCRONO, fecha risco 14)"
```

- [ ] **Step 6: Escrever `apps/agenda/tests/test_views_obter_consulta.py`**

```python
import time
from datetime import date

import httpx
import jwt as pyjwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import Client
from ulid import ULID

from apps.agenda import repository
from services.cerc.client import consultar_agenda
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
UFR_TESTE = "22333444000122"
URL_CERC = "https://ap-homolog.cerc.inf.br/v15/agenda/consultar"


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


def _limpar():
    db = get_db(FINANCIADOR_TESTE)
    parcial = {"data_liquidacao": "2026-09-20", "entidade_registradora": "22246686000196"}
    campos = ("data_liquidacao", "entidade_registradora")
    repository._com_filtros(db.table("consulta_agenda_ur").delete(), parcial, campos).execute()
    repository._com_filtros(db.table("agenda_ur_evento").delete(), parcial, campos).execute()
    repository._com_filtros(db.table("agenda_ur_pagamento").delete(), parcial, campos).execute()
    repository._com_filtros(db.table("agenda_ur").delete(), parcial, campos).execute()
    db.table("consulta_agenda").delete().eq("filtro_ufr", UFR_TESTE).execute()
    db.table("cerc_requisicao").delete().eq("recurso", "agenda_consultar").execute()
    db.table("politica_consulta").delete().eq("motivo", "TESTE-VIEW-OBTER").execute()


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch, keypair):
    _, public_pem = keypair
    monkeypatch.setenv("IAM_JWT_PUBLIC_KEY", public_pem)
    monkeypatch.setenv("IAM_JWT_ISSUER", "brikz-iam")
    monkeypatch.setenv("CERC_API_BASE_URL", "https://ap-homolog.cerc.inf.br")

    from services.cerc import client as cerc_client
    monkeypatch.setattr(cerc_client, "get_cerc_token", lambda financiador_id: "token-teste")
    monkeypatch.setattr(cerc_client, "invalidate_token", lambda financiador_id: None)

    _limpar()
    db = get_db(FINANCIADOR_TESTE)
    db.table("politica_consulta").insert({
        "id": str(ULID()), "motivo": "TESTE-VIEW-OBTER", "modos_permitidos": ["BATCH", "ONLINE"], "ativo": True,
    }).execute()
    yield
    _limpar()


def _consulta_teste(**overrides):
    base = {
        "modo": "BATCH", "documento_ufr": UFR_TESTE, "documento_titular": None,
        "credenciadoras": ["99T"], "arranjos": ["99T"],
        "data_inicio": date(2026, 9, 1), "data_fim": date(2026, 9, 30),
        "tipo_avaliacao": None, "participante": None, "carteira": None,
        "base_autorizativa": {"tipo": "OPTIN", "id": "opt_1"},
        "motivo": "TESTE-VIEW-OBTER", "ator": "teste@teste.com", "origem_ip": None,
    }
    base.update(overrides)
    return base


def _resposta_cerc_com_uma_ur():
    return [{
        "entidadeRegistradora": "22246686000196",
        "instituicaoCredenciadora": "36216798000150",
        "codigoArranjoPagamento": "VCC",
        "documentoUsuarioFinalRecebedor": UFR_TESTE,
        "unidadesRecebiveis": [{
            "dataLiquidacao": "2026-09-20",
            "constituicao": "1",
            "valorTotalUR": 1000.0,
            "titulares": [{
                "documentoTitular": UFR_TESTE,
                "valorConstituidoTotal": 1000.0,
                "dataHoraUltimaAtualizacao": "2026-09-19T10:00:00.000Z",
                "pagamentos": [],
            }],
        }],
    }]


def test_sem_jwt_retorna_401():
    response = Client().get("/api/v1/agendas/consultas/01HZZZZZZZZZZZZZZZZZZZZZZZ")
    assert response.status_code == 401


def test_consulta_nao_encontrada_retorna_404(keypair):
    private_pem, _ = keypair
    response = Client().get(
        "/api/v1/agendas/consultas/01HZZZZZZZZZZZZZZZZZZZZZZZ",
        HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 404


@respx.mock
def test_retorna_contagem_sincrono_e_frescor(keypair):
    private_pem, _ = keypair
    respx.post(URL_CERC).mock(return_value=httpx.Response(200, json=_resposta_cerc_com_uma_ur()))

    resultado = consultar_agenda(FINANCIADOR_TESTE, _consulta_teste())
    consulta_id = resultado["consultaId"]

    response = Client().get(
        f"/api/v1/agendas/consultas/{consulta_id}", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 200
    corpo = response.json()
    assert corpo["status"] == "COMPLETA"
    assert corpo["contagemPorOrigem"] == {"SINCRONO": 1, "WEBHOOK": 0, "ARQUIVO": 0}
    assert corpo["frescor"]["maisAntigo"] is not None
    assert corpo["frescor"]["maisRecente"] is not None


@respx.mock
def test_online_retorna_status_parcial_e_frescor_none_sem_urs(keypair):
    private_pem, _ = keypair
    respx.post(URL_CERC).mock(return_value=httpx.Response(200, json=[]))

    resultado = consultar_agenda(FINANCIADOR_TESTE, _consulta_teste(modo="ONLINE"))
    response = Client().get(
        f"/api/v1/agendas/consultas/{resultado['consultaId']}", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 200
    corpo = response.json()
    assert corpo["status"] == "PARCIAL"
    assert corpo["contagemPorOrigem"] == {"SINCRONO": 0, "WEBHOOK": 0, "ARQUIVO": 0}
    assert corpo["frescor"] is None
```

- [ ] **Step 7: Rodar os testes e confirmar que falham**

Run: `pytest apps/agenda/tests/test_views_obter_consulta.py -v`
Expected: FAIL — rota não existe

- [ ] **Step 8: Adicionar a view em `apps/agenda/views.py`**

Ajustar o import existente de `django.views.decorators.http` (linha `from django.views.decorators.http import require_POST`) para:

```python
from django.views.decorators.http import require_GET, require_POST
```

Adicionar aos imports:

```python
from apps.agenda.repository import _CHAVE_UR, _buscar_um
```

No final do arquivo:

```python
def _contagem_e_frescor_por_origem(db, consulta_id: str) -> tuple:
    """Única fonte de verdade pra contagem por origem desde que Task 2
    deste plano passou a gravar SINCRONO em consulta_agenda_ur (fecha
    design doc §15 risco 14) — WEBHOOK já era gravado pelo Plano 07.
    ARQUIVO fica sempre 0: a ingestão de arquivo (Plano 08) nunca associa
    uma UR a uma consulta_agenda, não existe consulta nesse fluxo."""
    vinculos = db.table("consulta_agenda_ur").select("*").eq("consulta_id", consulta_id).execute().data
    contagem = {"SINCRONO": 0, "WEBHOOK": 0, "ARQUIVO": 0}
    timestamps = []
    for vinculo in vinculos:
        contagem[vinculo["origem"]] = contagem.get(vinculo["origem"], 0) + 1
        chave = {campo: vinculo[campo] for campo in _CHAVE_UR}
        ur = _buscar_um(db, "agenda_ur", chave, _CHAVE_UR)
        if ur:
            timestamps.append(ur["data_hora_ultima_atualizacao"])

    frescor = None
    if timestamps:
        frescor = {"maisAntigo": min(timestamps).isoformat(), "maisRecente": max(timestamps).isoformat()}
    return contagem, frescor


def _serializar_consulta(consulta: dict, contagem: dict, frescor) -> dict:
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
        "contagemPorOrigem": contagem,
        "frescor": frescor,
        "iniciadaEm": consulta["iniciada_em"].isoformat(),
        "encerradaEm": consulta["encerrada_em"].isoformat() if consulta["encerrada_em"] else None,
    }


@jwt_required
@require_GET
def obter_consulta_agenda(request, consulta_id: str):
    """GET /api/v1/agendas/consultas/{id} (design doc §10, SPEC03 §7.2)."""
    db = get_db(request.financiador_id)
    linhas = db.table("consulta_agenda").select("*").eq("id", consulta_id).execute().data
    if not linhas:
        return JsonResponse(
            {"erro": "CONSULTA_NAO_ENCONTRADA", "mensagem": f"consulta {consulta_id!r} não encontrada"}, status=404,
        )

    contagem, frescor = _contagem_e_frescor_por_origem(db, consulta_id)
    return JsonResponse(_serializar_consulta(linhas[0], contagem, frescor), status=200)
```

- [ ] **Step 9: Registrar a rota em `apps/agenda/urls.py`**

```python
    path("agendas/consultas", views.criar_consulta_agenda),
    re_path(r"^agendas/consultas/(?P<consulta_id>[0-9A-Za-z]{26})$", views.obter_consulta_agenda),
```

- [ ] **Step 10: Rodar os testes e confirmar que passam**

Run: `pytest apps/agenda/tests/test_views_obter_consulta.py -v`
Expected: PASS (4 testes)

- [ ] **Step 11: Commit**

```bash
git add apps/agenda/views.py apps/agenda/urls.py apps/agenda/tests/test_views_obter_consulta.py
git commit -m "feat: endpoint GET /api/v1/agendas/consultas/{id} (Plano 09, Task 2)"
```

---

### Task 3: `GET/PUT/DELETE /api/v1/config/politicas-consulta`

**Files:**
- Modify: `apps/agenda/validation.py` (nova função `validar_modos_permitidos`)
- Modify: `apps/agenda/views.py` (novos imports; nova view `politicas_consulta`)
- Modify: `apps/agenda/urls.py` (nova rota)
- Test: `apps/agenda/tests/test_views_politicas_consulta.py`

**Interfaces:**
- Produces: `apps.agenda.validation.validar_modos_permitidos(modos_permitidos: list) -> None` (levanta `ValidacaoConsultaError`). `GET/PUT/DELETE /api/v1/config/politicas-consulta` — `401` sem JWT; `405` método fora de GET/PUT/DELETE; `400` corpo inválido, `motivo` ausente, ou `modosPermitidos` inválido; `404` (`DELETE` de motivo inexistente); sucesso: `GET` → `200 {"politicas": [...]}`; `PUT` → `200` com a política serializada (cria ou atualiza por `motivo`); `DELETE` → `200` com a política serializada, `ativo=false` (soft-delete, nunca remove a linha).

- [ ] **Step 1: Escrever `apps/agenda/tests/test_views_politicas_consulta.py`**

```python
import json
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import Client

from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
URL = "/api/v1/config/politicas-consulta"


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
    payload = {"exp": int(time.time()) + 300, "iss": "brikz-iam", "sub": "admin@teste.com", "financiador_id": FINANCIADOR_TESTE}
    return pyjwt.encode(payload, private_pem, algorithm="RS256")


def _limpar():
    get_db(FINANCIADOR_TESTE).table("politica_consulta").delete().eq("motivo", "TESTE-POLITICA-VIEW").execute()


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


def test_metodo_nao_permitido_retorna_405(keypair):
    private_pem, _ = keypair
    response = Client().post(URL, HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}")
    assert response.status_code == 405


def test_put_cria_politica_nova(keypair):
    private_pem, _ = keypair
    response = Client().put(
        URL, data=json.dumps({"motivo": "TESTE-POLITICA-VIEW", "modosPermitidos": ["BATCH", "ONLINE"]}),
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 200
    corpo = response.json()
    assert corpo["motivo"] == "TESTE-POLITICA-VIEW"
    assert corpo["modosPermitidos"] == ["BATCH", "ONLINE"]
    assert corpo["ativo"] is True


def test_put_atualiza_politica_existente_sem_duplicar(keypair):
    private_pem, _ = keypair
    Client().put(
        URL, data=json.dumps({"motivo": "TESTE-POLITICA-VIEW", "modosPermitidos": ["BATCH"]}),
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    response = Client().put(
        URL, data=json.dumps({"motivo": "TESTE-POLITICA-VIEW", "modosPermitidos": ["ONLINE"]}),
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 200
    assert response.json()["modosPermitidos"] == ["ONLINE"]

    db = get_db(FINANCIADOR_TESTE)
    linhas = db.table("politica_consulta").select("*").eq("motivo", "TESTE-POLITICA-VIEW").execute().data
    assert len(linhas) == 1


def test_put_modos_invalidos_retorna_400(keypair):
    private_pem, _ = keypair
    response = Client().put(
        URL, data=json.dumps({"motivo": "TESTE-POLITICA-VIEW", "modosPermitidos": ["ESTRANHO"]}),
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 400
    assert response.json()["erro"] == "MODOS_PERMITIDOS_INVALIDO"


def test_put_motivo_ausente_retorna_400(keypair):
    private_pem, _ = keypair
    response = Client().put(
        URL, data=json.dumps({"modosPermitidos": ["BATCH"]}),
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    assert response.status_code == 400


def test_get_lista_politicas(keypair):
    private_pem, _ = keypair
    Client().put(
        URL, data=json.dumps({"motivo": "TESTE-POLITICA-VIEW", "modosPermitidos": ["BATCH"]}),
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    response = Client().get(URL, HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}")
    assert response.status_code == 200
    motivos = [p["motivo"] for p in response.json()["politicas"]]
    assert "TESTE-POLITICA-VIEW" in motivos


def test_delete_desativa_politica_sem_apagar(keypair):
    private_pem, _ = keypair
    Client().put(
        URL, data=json.dumps({"motivo": "TESTE-POLITICA-VIEW", "modosPermitidos": ["BATCH"]}),
        content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}",
    )
    response = Client().delete(f"{URL}?motivo=TESTE-POLITICA-VIEW", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}")
    assert response.status_code == 200
    assert response.json()["ativo"] is False

    db = get_db(FINANCIADOR_TESTE)
    linha = db.table("politica_consulta").select("*").eq("motivo", "TESTE-POLITICA-VIEW").execute().data[0]
    assert linha["ativo"] is False


def test_delete_politica_inexistente_retorna_404(keypair):
    private_pem, _ = keypair
    response = Client().delete(f"{URL}?motivo=NAO-EXISTE", HTTP_AUTHORIZATION=f"Bearer {_token(private_pem)}")
    assert response.status_code == 404
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `pytest apps/agenda/tests/test_views_politicas_consulta.py -v`
Expected: FAIL — rota não existe

- [ ] **Step 3: Adicionar `validar_modos_permitidos` em `apps/agenda/validation.py`**

Adicionar próximo às outras funções `validar_*` (após `validar_politica_consulta`, por exemplo):

```python
_MODOS_VALIDOS = {"BATCH", "ONLINE"}


def validar_modos_permitidos(modos_permitidos) -> None:
    """Valida modos_permitidos <@ ARRAY['BATCH','ONLINE'] (design doc §7)
    — checagem de aplicação, não CHECK de banco (mesmo estilo dos irmãos:
    domínio muda por decisão de produto, não regra imutável do banco)."""
    if not modos_permitidos:
        raise ValidacaoConsultaError("MODOS_PERMITIDOS_VAZIO", "modosPermitidos não pode ser vazio")
    invalidos = [m for m in modos_permitidos if m not in _MODOS_VALIDOS]
    if invalidos:
        raise ValidacaoConsultaError(
            "MODOS_PERMITIDOS_INVALIDO", f"modosPermitidos contém valor(es) inválido(s): {', '.join(invalidos)}",
        )
```

- [ ] **Step 4: Adicionar a view em `apps/agenda/views.py`**

Ajustar o import de `django.views.decorators.http` (de novo) para incluir `require_http_methods`:

```python
from django.views.decorators.http import require_GET, require_http_methods, require_POST
```

Adicionar ao import já existente de `apps.agenda.validation`:

```python
from apps.agenda.validation import ValidacaoConsultaError, validar_modos_permitidos
```

No final do arquivo:

```python
def _serializar_politica(politica: dict) -> dict:
    return {
        "id": politica["id"],
        "motivo": politica["motivo"],
        "modosPermitidos": politica["modos_permitidos"],
        "ativo": politica["ativo"],
        "criadoEm": politica["criado_em"].isoformat(),
        "atualizadoEm": politica["atualizado_em"].isoformat(),
    }


def _listar_politicas(request):
    db = get_db(request.financiador_id)
    politicas = db.table("politica_consulta").select("*").execute().data
    return JsonResponse({"politicas": [_serializar_politica(p) for p in politicas]}, status=200)


def _upsert_politica(request):
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"erro": "CORPO_INVALIDO", "mensagem": "corpo não é JSON válido"}, status=400)

    motivo = payload.get("motivo")
    if not motivo:
        return JsonResponse({"erro": "CAMPO_OBRIGATORIO_AUSENTE", "mensagem": "motivo é obrigatório"}, status=400)

    try:
        validar_modos_permitidos(payload.get("modosPermitidos"))
    except ValidacaoConsultaError as exc:
        return JsonResponse({"erro": exc.codigo, "mensagem": exc.mensagem}, status=400)

    ativo = payload.get("ativo", True)
    db = get_db(request.financiador_id)
    dados = {
        "modos_permitidos": payload["modosPermitidos"], "ativo": ativo,
        "atualizado_em": datetime.now(timezone.utc),
    }
    existente = db.table("politica_consulta").select("id").eq("motivo", motivo).execute().data
    if existente:
        politica = db.table("politica_consulta").update(dados).eq("motivo", motivo).execute().data[0]
    else:
        politica = db.table("politica_consulta").insert({"id": str(ULID()), "motivo": motivo, **dados}).execute().data[0]

    return JsonResponse(_serializar_politica(politica), status=200)


def _desativar_politica(request):
    motivo = request.GET.get("motivo")
    if not motivo:
        return JsonResponse(
            {"erro": "CAMPO_OBRIGATORIO_AUSENTE", "mensagem": "motivo é obrigatório (query string)"}, status=400,
        )

    db = get_db(request.financiador_id)
    existente = db.table("politica_consulta").select("*").eq("motivo", motivo).execute().data
    if not existente:
        return JsonResponse(
            {"erro": "POLITICA_NAO_ENCONTRADA", "mensagem": f"nenhuma política para o motivo {motivo!r}"}, status=404,
        )

    politica = db.table("politica_consulta").update({
        "ativo": False, "atualizado_em": datetime.now(timezone.utc),
    }).eq("motivo", motivo).execute().data[0]
    return JsonResponse(_serializar_politica(politica), status=200)


@jwt_required
@require_http_methods(["GET", "PUT", "DELETE"])
def politicas_consulta(request):
    """GET/PUT/DELETE /api/v1/config/politicas-consulta (design doc §7)
    — self-service: cada financiador só vê/edita a própria política,
    automático porque cada um tem seu próprio banco (nenhuma checagem de
    propriedade extra é necessária). DELETE nunca apaga a linha — só
    desativa (Global Constraints deste plano)."""
    if request.method == "GET":
        return _listar_politicas(request)
    if request.method == "PUT":
        return _upsert_politica(request)
    return _desativar_politica(request)
```

- [ ] **Step 5: Registrar a rota em `apps/agenda/urls.py`**

```python
    path("config/politicas-consulta", views.politicas_consulta),
```

- [ ] **Step 6: Rodar os testes e confirmar que passam**

Run: `pytest apps/agenda/tests/test_views_politicas_consulta.py -v`
Expected: PASS (8 testes)

- [ ] **Step 7: Rodar a suíte inteira do app para checar regressão**

Run: `pytest apps/agenda services/cerc -v`
Expected: PASS em todos os testes (Planos 01-09)

- [ ] **Step 8: Commit**

```bash
git add apps/agenda/validation.py apps/agenda/views.py apps/agenda/urls.py apps/agenda/tests/test_views_politicas_consulta.py
git commit -m "feat: CRUD self-service de politica_consulta (Plano 09, Task 3)"
```
