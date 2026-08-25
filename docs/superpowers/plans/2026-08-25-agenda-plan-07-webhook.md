# agenda-service — Plan 07: Webhook + Correlação + Pub/Sub + Job de Completude — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Receber o webhook `tipoEvento=agenda` da CERC, publicar em Pub/Sub, correlacionar cada UR recebida contra as consultas ONLINE em `PARCIAL` (SPEC03 §5.4), persistir via `upsert_agenda_ur` (origem `WEBHOOK`), e fechar consultas via um job de completude (quiet period 90s / hard timeout 15min, SPEC03 §5.5).

**Architecture:** Quatro tasks. Task 1 copia a infra de Pub/Sub (`shared/pubsub_client.py`, `shared/pubsub_auth.py`) de `ap-back-contratos` (mesmo padrão de cópia entre serviços já estabelecido) e o receptor HTTP fino (`webhook_agenda`). Task 2 é o algoritmo de correlação, puro e testável isoladamente. Task 3 é o consumidor da push subscription, que usa a correlação da Task 2 e o `upsert_agenda_ur` do Plano 05. Task 4 é o job de completude, um endpoint HTTP disparado por Cloud Scheduler (mesma verificação OIDC do consumidor Pub/Sub).

**Tech Stack:** `google-cloud-pubsub` (novo), `google-auth` (transitivo, já usado pela verificação OIDC). Nenhuma outra dependência nova.

**Spec:** `docs/superpowers/specs/2026-08-24-agenda-service-design.md` §8 (webhook, Pub/Sub, completude), §6 (roteamento de tenant). `docs/specs/SPEC-03-consulta-de-agenda-ap005.md` §5 (webhook completo: comportamento, payload, receptor, correlação, completude, teste em homologação). Referência de implementação: `ap-back-contratos/contratos/apps/contratos/views.py` + `shared/pubsub_client.py` + `shared/pubsub_auth.py` (webhook `tipoEvento=contrato`, já revisado e em produção — mesmo molde estrutural, adaptado para o payload e a correlação heurística de agenda). Série: plano 7 de ~10.

**Depends on:** `2026-08-24-agenda-plan-03-cloudsql-client.md`, `2026-08-25-agenda-plan-05-upsert-repository.md` (`upsert_agenda_ur`).

## Global Constraints

- **Autenticação do receptor:** Basic Auth contra `webhook_basic_user`/`webhook_basic_password` em `TENANT_{financiador_id}_CONFIG` — chaves novas, aditivas (mesmo padrão do `participante_tipo` do Plano 06). `financiador_id` vem embutido na URL (`/api/v1/webhooks/agenda/{financiador_id}`), não do JWT — a CERC chama este endpoint diretamente, não há JWT do IdP corporativo aqui.
- **`webhook_agenda` é fino por design:** autentica, grava em `webhook_inbox`, publica no Pub/Sub, responde. Nenhuma correlação/upsert acontece na requisição síncrona da CERC — isso é do consumidor da push subscription (Task 3), que a CERC nunca chama diretamente.
- **Dedupe por `hash_dedupe`** (`UNIQUE` em `webhook_inbox`, já existe desde o Plano 02) — uma reentrega da CERC do mesmo evento retorna `202` sem duplicar a linha nem publicar de novo.
- **Idempotência do consumidor:** guardada por `webhook_inbox.processado_em IS NULL` — uma redelivery do Pub/Sub (at-least-once) some no primeiro `if inbox["processado_em"] is not None: return 204`, antes de qualquer escrita em `agenda_ur`/`agenda_ur_orfa`/`consulta_agenda_ur`. Isso também significa que `consulta_agenda.qtd_urs_webhook`/`ultima_ur_em` só são tocados uma vez por webhook_inbox.
- **Correlação (SPEC03 §5.4):** casa `(instituicaoCredenciadora, codigoArranjoPagamento, documentoUsuarioFinalRecebedor, documentoTitular, dataLiquidacao)` do evento contra `consulta_agenda WHERE status='PARCIAL' AND modo='ONLINE'`, tratando `99T` como universo (em qualquer lado — filtro ou evento, aqui só o filtro pode ser `99T`, o evento sempre vem com valores concretos da CERC) e a janela `filtro_data_inicio..filtro_data_fim` como intervalo fechado. Zero casamentos → `agenda_ur_orfa` (nunca descarta). Um ou mais → `consulta_agenda_ur` para cada consulta casada, e o UR é persistido uma única vez (`upsert_agenda_ur` não duplica por consulta casada).
- **`qtd_urs_webhook` não é incrementado atomicamente** — é um `SELECT` seguido de `UPDATE ... SET qtd_urs_webhook = <valor lido> + 1`, não uma expressão SQL `+1` (o `QueryBuilder` de `shared/cloudsql_client.py` não suporta updates baseados em expressão — mesma classe de limitação já registrada no design doc §15 risco 1). Sob rajada real de webhooks concorrentes para a mesma consulta (SPEC03 §5.3: "milhares de requisições" em pouco tempo), incrementos podem se perder. Não corrigir agora — `consulta_agenda_ur` (a fonte de verdade para "quantas URs casaram") não tem esse problema, já que cada `INSERT` é independente; `qtd_urs_webhook` é só um contador de conveniência exibido em `GET /api/v1/agendas/consultas/{id}` (Plano 09), que pode preferir `COUNT(*) FROM consulta_agenda_ur` em vez da coluna se isso importar.
- **Lista de tenants do job de completude:** fixa (`["12345678000199"]`), mesmo ponto em aberto do design doc §14 item 3 — não resolvido por este plano.
- **`PUBSUB_PUSH_AUDIENCE`/`PUBSUB_PUSH_INVOKER_SA`** protegem tanto o consumidor do webhook quanto o job de completude — os dois são endpoints internos batidos por uma identidade de serviço do Google (Pub/Sub push e Cloud Scheduler, respectivamente, ambos usam o mesmo mecanismo de OIDC token assinado pelo Google) — uma única verificação (`shared/pubsub_auth.verificar_push_oidc`) serve aos dois, apesar do nome do módulo mencionar só Pub/Sub.

---

### Task 1: Pub/Sub (`shared/pubsub_client.py`, `shared/pubsub_auth.py`) + receptor do webhook

**Files:**
- Create: `shared/pubsub_client.py`
- Create: `shared/pubsub_auth.py`
- Create: `apps/agenda/webhook_dedupe.py`
- Modify: `apps/agenda/views.py` (adiciona `webhook_agenda`)
- Modify: `apps/agenda/urls.py` (registra a rota)
- Modify: `requirements.txt` (adiciona `google-cloud-pubsub`)
- Modify: `.env.example` (documenta as novas env vars)
- Test: `apps/agenda/tests/test_views_webhook.py`

**Interfaces:**
- Produces: `shared.pubsub_client.publish_webhook_agenda(webhook_inbox_id: str, financiador_id: str) -> None` (melhor-esforço, nunca lança); `shared.pubsub_auth.verificar_push_oidc(request) -> bool`; `apps.agenda.webhook_dedupe.hash_evento(tipo_evento: str, evento: dict, data_hora_evento: str) -> str`. Consumido pela Task 3 (consumidor) e pela Task 4 (job de completude, só `verificar_push_oidc`).

- [ ] **Step 1: Escrever `shared/pubsub_auth.py`** (idêntico ao de `ap-back-contratos`, genérico — não tem nada específico de contrato/agenda)

```python
"""Verificação OIDC do push subscription do Pub/Sub — design §6 ("Push
subscription bate em endpoint próprio, verificado por OIDC"). O Pub/Sub
assina cada requisição de push com um ID token OIDC do Google, emitido
para a conta de serviço configurada na subscription; verificamos aqui que
o token é genuíno e foi emitido para esta audiência específica — defesa
em profundidade além do IAM do próprio Cloud Run (roles/run.invoker
restrito à conta do Pub/Sub). O mesmo mecanismo protege o job de
completude (Task 4) disparado por Cloud Scheduler — o nome do módulo
menciona só Pub/Sub, mas a verificação é genérica.
"""

import logging
import os

logger = logging.getLogger(__name__)


def _verificar_id_token(token: str, audiencia: str) -> dict:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, GoogleAuthRequest(), audience=audiencia)


def verificar_push_oidc(request) -> bool:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer "):
        return False
    token = header[len("Bearer "):]

    try:
        audiencia = os.environ["PUBSUB_PUSH_AUDIENCE"]
        claims = _verificar_id_token(token, audiencia)
    except Exception:
        logger.warning("[Webhook] Token OIDC do push inválido ou PUBSUB_PUSH_AUDIENCE não configurado")
        return False

    conta_esperada = os.getenv("PUBSUB_PUSH_INVOKER_SA")
    if conta_esperada and claims.get("email") != conta_esperada:
        logger.warning("[Webhook] Token OIDC de conta inesperada: %s", claims.get("email"))
        return False

    return True
```

- [ ] **Step 2: Escrever `shared/pubsub_client.py`**

```python
"""Publish no tópico de webhook_inbox — design §8. O handler HTTP
(apps/agenda/views.py) já gravou o evento cru em webhook_inbox ANTES de
chamar isto. Publicar aqui é melhor-esforço: se falhar (rede, projeto GCP
não configurado em dev, o que for), a linha já persistida continua lá com
processado_em IS NULL, e um job de varredura futuro a recuperaria. Por
isso esta função nunca deixa uma exceção escapar — ela só loga.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_publisher = None


def _get_publisher():
    global _publisher
    if _publisher is None:
        from google.cloud import pubsub_v1

        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def _topic_path() -> str:
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    topic = os.getenv("PUBSUB_TOPIC_AGENDA_WEBHOOK", "agenda-webhook-inbox")
    return f"projects/{project}/topics/{topic}"


def publish_webhook_agenda(webhook_inbox_id: str, financiador_id: str) -> None:
    """Publica só os IDs (não o payload) — o consumidor busca o evento
    completo em webhook_inbox pelo id; a mensagem em si fica pequena e o
    payload nunca vive em dois lugares."""
    try:
        topic = _topic_path()
        data = json.dumps({
            "webhook_inbox_id": webhook_inbox_id,
            "financiador_id": financiador_id,
        }).encode("utf-8")
        future = _get_publisher().publish(topic, data)
        future.add_done_callback(lambda f: _log_publish_result(f, webhook_inbox_id))
    except Exception:
        logger.exception("[Pub/Sub] Falha ao publicar webhook_inbox_id=%s", webhook_inbox_id)


def _log_publish_result(future, webhook_inbox_id: str) -> None:
    try:
        future.result()
    except Exception:
        logger.exception("[Pub/Sub] Publish assíncrono falhou webhook_inbox_id=%s", webhook_inbox_id)
```

- [ ] **Step 3: Escrever `apps/agenda/webhook_dedupe.py`**

```python
"""Hash de deduplicação de webhooks — SPEC03 §5.3: dedupe por (tipoEvento,
hash canônico do evento, dataHoraEvento). A CERC reentrega o mesmo evento
em até 5 tentativas quando não recebe 2xx; reentrega deve ser inofensiva."""

import hashlib
import json


def hash_evento(tipo_evento: str, evento: dict, data_hora_evento: str) -> str:
    canonico = json.dumps(evento, sort_keys=True, ensure_ascii=False, default=str)
    chave = f"{tipo_evento}|{data_hora_evento}|{canonico}"
    return hashlib.sha256(chave.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Adicionar `webhook_agenda` a `apps/agenda/views.py`**

Adicione estes imports no topo do arquivo (mantenha `from django.http import JsonResponse` já existente) e as funções abaixo, deixando `health` como está:

```python
import base64
import hmac
import json
import logging
from datetime import datetime

from django.http import JsonResponse
from django.views.decorators.http import require_POST
from sqlalchemy.exc import DBAPIError
from ulid import ULID

from apps.agenda.webhook_dedupe import hash_evento
from shared import pubsub_client
from shared.cloudsql_client import get_db
from shared.tenant_config import get_tenant_config

logger = logging.getLogger(__name__)


def health(request):
    return JsonResponse({"status": "ok"})


def _violacao_unique(erro: DBAPIError) -> bool:
    """True se o DBAPIError é uma violação de constraint UNIQUE (sqlstate
    23505) — pg8000 não popula os subtipos de exceção da PEP 249, então
    toda falha do protocolo chega como DatabaseError genérico; o único
    jeito confiável de distinguir "duplicado" de "outro erro de banco" é
    inspecionar o dicionário de campos da mensagem de erro do Postgres."""
    args = getattr(erro.orig, "args", None)
    return bool(args) and isinstance(args[0], dict) and args[0].get("C") == "23505"


def _autenticado(request, financiador_id: str) -> bool:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Basic "):
        return False
    try:
        decodificado = base64.b64decode(header[len("Basic "):]).decode("utf-8")
    except Exception:
        return False
    usuario, _, senha = decodificado.partition(":")

    try:
        config = get_tenant_config(financiador_id)
    except Exception:
        return False

    usuario_esperado = config.get("webhook_basic_user")
    senha_esperada = config.get("webhook_basic_password")
    if not usuario_esperado or not senha_esperada:
        logger.error("[Webhook] Credenciais Basic não configuradas para o tenant %s", financiador_id)
        return False

    ok_usuario = hmac.compare_digest(usuario.encode("utf-8"), usuario_esperado.encode("utf-8"))
    ok_senha = hmac.compare_digest(senha.encode("utf-8"), senha_esperada.encode("utf-8"))
    return ok_usuario and ok_senha


@require_POST
def webhook_agenda(request, financiador_id: str):
    """Receptor do webhook CERC (tipoEvento=agenda) — SPEC03 §5.2/§5.3.

    Fino por design: autentica, grava em webhook_inbox, publica no Pub/Sub
    e responde. Nenhuma correlação/upsert acontece aqui — isso é do
    consumidor da push subscription (processar_webhook_agenda)."""
    if not _autenticado(request, financiador_id):
        return JsonResponse({"erro": "autenticação inválida"}, status=401)

    try:
        envelope = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"erro": "corpo não é JSON válido"}, status=400)

    tipo_evento = envelope.get("tipoEvento") if isinstance(envelope, dict) else None
    data_hora_evento = envelope.get("dataHoraEvento") if isinstance(envelope, dict) else None
    evento = envelope.get("evento") if isinstance(envelope, dict) else None
    if not tipo_evento or not data_hora_evento or evento is None:
        return JsonResponse(
            {"erro": "envelope inválido: tipoEvento, dataHoraEvento e evento são obrigatórios"}, status=400,
        )

    hash_dedupe = hash_evento(tipo_evento, evento, data_hora_evento)
    webhook_id = str(ULID())

    try:
        get_db(financiador_id).table("webhook_inbox").insert({
            "id": webhook_id,
            "tipo_evento": tipo_evento,
            "data_hora_evento": datetime.fromisoformat(data_hora_evento),
            "payload": envelope,
            "hash_dedupe": hash_dedupe,
        }).execute()
    except DBAPIError as erro:
        if _violacao_unique(erro):
            logger.info("[Webhook] Evento duplicado ignorado (financiador=%s, hash=%s)", financiador_id, hash_dedupe)
            return JsonResponse({}, status=202)
        logger.exception("[Webhook] Falha ao persistir webhook_inbox (financiador=%s)", financiador_id)
        return JsonResponse({"erro": "falha ao persistir evento"}, status=500)
    except Exception:
        logger.exception("[Webhook] Falha ao persistir webhook_inbox (financiador=%s)", financiador_id)
        return JsonResponse({"erro": "falha ao persistir evento"}, status=500)

    try:
        pubsub_client.publish_webhook_agenda(webhook_id, financiador_id)
    except Exception:
        logger.exception("[Webhook] publish_webhook_agenda levantou inesperadamente (financiador=%s)", financiador_id)

    return JsonResponse({}, status=202)
```

- [ ] **Step 5: Registrar a rota em `apps/agenda/urls.py`**

```python
from django.urls import path, re_path
from . import views

urlpatterns = [
    path("health", views.health),
    re_path(r"^webhooks/agenda/(?P<financiador_id>\d{14})$", views.webhook_agenda),
]
```

- [ ] **Step 6: Adicionar `google-cloud-pubsub` a `requirements.txt`**

Adicione a linha `google-cloud-pubsub` (mesma versão implícita do `ap-back-contratos`, sem pin).

- [ ] **Step 7: Documentar as novas env vars em `.env.example`**

Adicione ao final do arquivo:

```
# Pub/Sub — webhook de agenda (Plano 07). GOOGLE_CLOUD_PROJECT não é
# setado em dev local (publish vira melhor-esforço e falha silenciosamente,
# só logado — ver shared/pubsub_client.py). PUBSUB_PUSH_AUDIENCE protege
# tanto o consumidor do webhook quanto o job de completude (mesmo
# mecanismo OIDC, dois disparadores diferentes: Pub/Sub push e Cloud
# Scheduler). PUBSUB_PUSH_INVOKER_SA é opcional (checagem extra de
# identidade, além da audiência).
PUBSUB_TOPIC_AGENDA_WEBHOOK=agenda-webhook-inbox
PUBSUB_PUSH_AUDIENCE=https://agenda-homolog.example.com/api/v1/webhooks/agenda/processar
PUBSUB_PUSH_INVOKER_SA=
```

- [ ] **Step 8: Escrever `apps/agenda/tests/test_views_webhook.py`**

```python
import base64
import json

import pytest
from django.test import Client
from sqlalchemy.exc import DBAPIError

from apps.agenda import views
from apps.agenda.webhook_dedupe import hash_evento
from shared import pubsub_client
from shared.cloudsql_client import get_db
from shared.tenant_config import get_tenant_config

FINANCIADOR_TESTE = "12345678000199"
URL = f"/api/v1/webhooks/agenda/{FINANCIADOR_TESTE}"


def _basic_auth_header():
    config = get_tenant_config(FINANCIADOR_TESTE)
    credenciais = f"{config['webhook_basic_user']}:{config['webhook_basic_password']}"
    return "Basic " + base64.b64encode(credenciais.encode()).decode()


def _envelope(documento_ufr, data_hora="2026-08-17T12:00:00.000Z"):
    return {
        "tipoEvento": "agenda",
        "dataHoraEvento": data_hora,
        "evento": {
            "entidadeRegistradora": "22246686000196",
            "instituicaoCredenciadora": "36216798000150",
            "documentoUsuarioFinalRecebedor": documento_ufr,
            "codigoArranjoPagamento": "VCC",
            "documentoTitular": documento_ufr,
            "dataLiquidacao": "2026-09-20",
            "constituicao": "1",
            "valorConstituidoTotal": 1000.0,
            "valorTotalUR": 1000.0,
            "dataHoraUltimaAtualizacao": "2026-08-17T04:58:36.087Z",
            "pagamentos": [],
        },
    }


def _limpar(envelope):
    h = hash_evento(envelope["tipoEvento"], envelope["evento"], envelope["dataHoraEvento"])
    get_db(FINANCIADOR_TESTE).table("webhook_inbox").delete().eq("hash_dedupe", h).execute()


@pytest.fixture
def publicados(monkeypatch):
    chamadas = []
    monkeypatch.setattr(pubsub_client, "publish_webhook_agenda", lambda *a, **k: chamadas.append(a))
    return chamadas


def test_webhook_sem_autenticacao_retorna_401(publicados):
    response = Client().post(URL, data=json.dumps(_envelope("11222333000181")), content_type="application/json")
    assert response.status_code == 401
    assert publicados == []


def test_webhook_com_credenciais_erradas_retorna_401(publicados):
    response = Client().post(
        URL, data=json.dumps(_envelope("11222333000181")), content_type="application/json",
        HTTP_AUTHORIZATION="Basic " + base64.b64encode(b"errado:errado").decode(),
    )
    assert response.status_code == 401
    assert publicados == []


def test_webhook_get_retorna_405():
    response = Client().get(URL)
    assert response.status_code == 405


def test_webhook_corpo_nao_json_retorna_400():
    response = Client().post(
        URL, data="isto nao e json", content_type="text/plain", HTTP_AUTHORIZATION=_basic_auth_header(),
    )
    assert response.status_code == 400


def test_webhook_envelope_sem_campos_obrigatorios_retorna_400():
    response = Client().post(
        URL, data=json.dumps({"tipoEvento": "agenda"}), content_type="application/json",
        HTTP_AUTHORIZATION=_basic_auth_header(),
    )
    assert response.status_code == 400


def test_webhook_valido_persiste_no_inbox_e_publica(publicados):
    envelope = _envelope("11222333000181")
    _limpar(envelope)
    try:
        response = Client().post(
            URL, data=json.dumps(envelope), content_type="application/json",
            HTTP_AUTHORIZATION=_basic_auth_header(),
        )
        assert response.status_code == 202

        h = hash_evento(envelope["tipoEvento"], envelope["evento"], envelope["dataHoraEvento"])
        salvo = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("hash_dedupe", h).execute()
        assert len(salvo.data) == 1
        assert salvo.data[0]["tipo_evento"] == "agenda"
        assert salvo.data[0]["payload"] == envelope
        assert salvo.data[0]["processado_em"] is None

        assert len(publicados) == 1
        assert publicados[0][1] == FINANCIADOR_TESTE
    finally:
        _limpar(envelope)


def test_webhook_duplicado_nao_gera_segunda_linha_nem_publica_de_novo(publicados):
    envelope = _envelope("11222333000181")
    _limpar(envelope)
    try:
        cliente = Client()
        r1 = cliente.post(URL, data=json.dumps(envelope), content_type="application/json", HTTP_AUTHORIZATION=_basic_auth_header())
        r2 = cliente.post(URL, data=json.dumps(envelope), content_type="application/json", HTTP_AUTHORIZATION=_basic_auth_header())

        assert r1.status_code == 202
        assert r2.status_code == 202

        h = hash_evento(envelope["tipoEvento"], envelope["evento"], envelope["dataHoraEvento"])
        salvos = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("hash_dedupe", h).execute()
        assert len(salvos.data) == 1
        assert len(publicados) == 1
    finally:
        _limpar(envelope)


def test_webhook_responde_202_mesmo_quando_publish_falha(monkeypatch):
    envelope = _envelope("11222333000181")
    _limpar(envelope)

    def _falha(*args, **kwargs):
        raise RuntimeError("Pub/Sub indisponível")

    monkeypatch.setattr(pubsub_client, "publish_webhook_agenda", _falha)
    try:
        response = Client().post(URL, data=json.dumps(envelope), content_type="application/json", HTTP_AUTHORIZATION=_basic_auth_header())
        assert response.status_code == 202

        h = hash_evento(envelope["tipoEvento"], envelope["evento"], envelope["dataHoraEvento"])
        salvo = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("hash_dedupe", h).execute()
        assert len(salvo.data) == 1
    finally:
        _limpar(envelope)
```

**Nota para o implementador:** `_basic_auth_header()` lê `webhook_basic_user`/`webhook_basic_password` de `TENANT_12345678000199_CONFIG` — confirme que o `.env` local já tem essas chaves na config desse tenant antes de rodar os testes (é uma config aditiva de tenant, não uma env var solta); se estiverem ausentes, adicione-as ao JSON do `.env` local (não commitado) antes de continuar — sem inventar um valor, pergunte se não achar onde estão documentadas.

- [ ] **Step 9: Rodar a suíte**

Run: `pytest apps/agenda/tests/test_views_webhook.py -v`
Expected: PASS em todos os testes.

- [ ] **Step 10: Commit**

```bash
git add shared/pubsub_client.py shared/pubsub_auth.py apps/agenda/webhook_dedupe.py apps/agenda/views.py apps/agenda/urls.py apps/agenda/tests/test_views_webhook.py requirements.txt .env.example
git commit -m "feat: receptor do webhook de agenda (Pub/Sub, copiado de ap-back-contratos)"
```

---

### Task 2: `apps/agenda/correlacao.py`

**Files:**
- Create: `apps/agenda/correlacao.py`
- Test: `apps/agenda/tests/test_correlacao.py`

**Interfaces:**
- Consumes: `shared.cloudsql_client.get_db`.
- Produces: `encontrar_consultas_candidatas(financiador_id: str, evento: dict) -> list[dict]` — lista de linhas de `consulta_agenda` (dicts completos) cujos filtros contêm o evento. Consumida pela Task 3.

- [ ] **Step 1: Escrever `apps/agenda/correlacao.py`**

```python
"""Correlação webhook <-> consulta (SPEC03 §5.4) — o payload do webhook não
carrega um identificador da consulta que o originou, então a correlação é
reconstruída pela chave de negócio: (instituicaoCredenciadora,
codigoArranjoPagamento, documentoUsuarioFinalRecebedor, documentoTitular,
dataLiquidacao), casada contra consultas ONLINE ainda em PARCIAL, tratando
'99T' no filtro como universo e a janela de datas como intervalo fechado.
"""
from datetime import date

from shared.cloudsql_client import get_db


def _lista_contem(lista, valor: str) -> bool:
    return "99T" in lista or valor in lista


def encontrar_consultas_candidatas(financiador_id: str, evento: dict) -> list:
    db = get_db(financiador_id)
    candidatas = (
        db.table("consulta_agenda").select("*")
        .eq("status", "PARCIAL").eq("modo", "ONLINE")
        .execute().data
    )

    documento_ufr = evento["documentoUsuarioFinalRecebedor"]
    documento_titular = evento.get("documentoTitular")
    credenciadora = evento["instituicaoCredenciadora"]
    arranjo = evento["codigoArranjoPagamento"]
    data_liquidacao = date.fromisoformat(evento["dataLiquidacao"])

    casadas = []
    for consulta in candidatas:
        if consulta["filtro_ufr"] != documento_ufr:
            continue
        if consulta["filtro_titular"] and documento_titular and consulta["filtro_titular"] != documento_titular:
            continue
        if not _lista_contem(consulta["filtro_credenciadoras"], credenciadora):
            continue
        if not _lista_contem(consulta["filtro_arranjos"], arranjo):
            continue
        if not (consulta["filtro_data_inicio"] <= data_liquidacao <= consulta["filtro_data_fim"]):
            continue
        casadas.append(consulta)
    return casadas
```

- [ ] **Step 2: Escrever `apps/agenda/tests/test_correlacao.py`**

```python
from datetime import date, datetime, timezone

import pytest

from apps.agenda.correlacao import encontrar_consultas_candidatas
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
UFR_TESTE = "33333333000133"


def _consulta(id_, **overrides):
    base = {
        "id": id_,
        "modo": "ONLINE",
        "status": "PARCIAL",
        "filtro_ufr": UFR_TESTE,
        "filtro_titular": None,
        "filtro_credenciadoras": ["99T"],
        "filtro_arranjos": ["99T"],
        "filtro_data_inicio": date(2026, 9, 1),
        "filtro_data_fim": date(2026, 9, 30),
        "base_autorizativa_tipo": "OPTIN",
        "base_autorizativa_id": "opt_1",
        "motivo": "TESTE-CORRELACAO",
        "ator": "teste@teste.com",
    }
    base.update(overrides)
    return base


def _evento(**overrides):
    base = {
        "entidadeRegistradora": "22246686000196",
        "instituicaoCredenciadora": "36216798000150",
        "documentoUsuarioFinalRecebedor": UFR_TESTE,
        "codigoArranjoPagamento": "VCC",
        "documentoTitular": UFR_TESTE,
        "dataLiquidacao": "2026-09-20",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _limpar():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").delete().eq("filtro_ufr", UFR_TESTE).execute()
    yield
    db.table("consulta_agenda").delete().eq("filtro_ufr", UFR_TESTE).execute()


def test_casa_por_curinga_em_credenciadora_e_arranjo():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta("corr-1")).execute()

    casadas = encontrar_consultas_candidatas(FINANCIADOR_TESTE, _evento())

    assert [c["id"] for c in casadas] == ["corr-1"]


def test_nao_casa_credenciadora_especifica_diferente():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta("corr-2", filtro_credenciadoras=["11111111000100"])).execute()

    casadas = encontrar_consultas_candidatas(FINANCIADOR_TESTE, _evento())

    assert casadas == []


def test_nao_casa_fora_da_janela_de_datas():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta(
        "corr-3", filtro_data_inicio=date(2026, 1, 1), filtro_data_fim=date(2026, 1, 31),
    )).execute()

    casadas = encontrar_consultas_candidatas(FINANCIADOR_TESTE, _evento())

    assert casadas == []


def test_casa_multiplas_consultas_simultaneamente():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta("corr-4a")).execute()
    db.table("consulta_agenda").insert(_consulta("corr-4b", motivo="OUTRO-MOTIVO")).execute()

    casadas = encontrar_consultas_candidatas(FINANCIADOR_TESTE, _evento())

    assert {c["id"] for c in casadas} == {"corr-4a", "corr-4b"}


def test_ignora_consulta_batch_e_consulta_ja_completa():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta("corr-5", modo="BATCH")).execute()
    db.table("consulta_agenda").insert(_consulta("corr-6", status="COMPLETA")).execute()

    casadas = encontrar_consultas_candidatas(FINANCIADOR_TESTE, _evento())

    assert casadas == []


def test_filtro_titular_especifico_so_casa_o_mesmo_titular():
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta("corr-7", filtro_titular="99999999000191")).execute()

    casadas = encontrar_consultas_candidatas(FINANCIADOR_TESTE, _evento(documentoTitular=UFR_TESTE))

    assert casadas == []
```

- [ ] **Step 3: Rodar a suíte**

Run: `pytest apps/agenda/tests/test_correlacao.py -v`
Expected: PASS em todos os testes.

- [ ] **Step 4: Commit**

```bash
git add apps/agenda/correlacao.py apps/agenda/tests/test_correlacao.py
git commit -m "feat: algoritmo de correlacao webhook-consulta (SPEC03 par.5.4)"
```

---

### Task 3: Consumidor do webhook (`processar_webhook_agenda`)

**Files:**
- Modify: `apps/agenda/views.py` (adiciona `processar_webhook_agenda`)
- Modify: `apps/agenda/urls.py` (registra a rota)
- Test: `apps/agenda/tests/test_views_webhook_processor.py`

**Interfaces:**
- Consumes: `apps.agenda.correlacao.encontrar_consultas_candidatas` (Task 2), `apps.agenda.repository.upsert_agenda_ur` (Plano 05), `shared.pubsub_auth.verificar_push_oidc` (Task 1).
- Produces: view `processar_webhook_agenda` — endpoint interno da push subscription, nunca chamado pela CERC diretamente.

- [ ] **Step 1: Adicionar a `apps/agenda/views.py`**

Adicione estes imports (junto aos já existentes de Task 1) e a função abaixo:

```python
from datetime import timezone

from apps.agenda.correlacao import encontrar_consultas_candidatas
from apps.agenda.repository import upsert_agenda_ur
from shared.pubsub_auth import verificar_push_oidc
```

(o `from datetime import datetime` de Task 1 já existe — só adicione `timezone` a essa mesma linha: `from datetime import datetime, timezone`.)

```python
def _traduzir_pagamento_webhook(pagamento: dict) -> dict:
    return {
        "tipo_informacao_pagamento": str(pagamento["tipoInformacaoPagamento"]),
        "indicador_efeitos_contrato": pagamento.get("indicadorEfeitosContrato"),
        "identificador_cerc_contrato": None,  # coluna 12.16 — só o Plano 08 (arquivo AP005) preenche
        "regras_divisao": pagamento.get("regrasDivisao"),
        "valor_onerado": pagamento.get("valorOnerado"),
        "valor_constituido_efeito": pagamento.get("valorConstituidoEfeito"),
        "valor_a_pagar": pagamento.get("valorAPagar"),
        "beneficiario": pagamento.get("beneficiario"),
        "data_liquidacao_efetiva": pagamento.get("dataLiquidacaoEfetiva"),
        "valor_liquidacao_efetiva": pagamento.get("valorLiquidacaoEfetiva"),
        "motivo_nao_pagamento": pagamento.get("motivoDeNaoPagamento"),
        "domicilio": pagamento.get("domicilioPagamento") or {},
    }


def _traduzir_evento_webhook(evento: dict) -> tuple:
    cabecalho = {
        "entidade_registradora": evento["entidadeRegistradora"],
        "cnpj_credenciadora": evento["instituicaoCredenciadora"],
        "codigo_arranjo": evento["codigoArranjoPagamento"],
        "documento_ufr": evento["documentoUsuarioFinalRecebedor"],
        "documento_titular": evento["documentoTitular"],
        "data_liquidacao": evento["dataLiquidacao"],
        "constituicao": evento["constituicao"],
        "valor_constituido_total": evento["valorConstituidoTotal"],
        "valor_constituido_antecipacao_pre": evento.get("valorConstituidoAntecipacaoPre", 0),
        "valor_bloqueado": evento.get("valorBloqueado", 0),
        "valor_livre": evento.get("valorLivre", 0),
        "valor_total_ur": evento["valorTotalUR"],
        "carteira": evento.get("carteira"),
        "data_hora_ultima_atualizacao": datetime.fromisoformat(evento["dataHoraUltimaAtualizacao"].replace("Z", "+00:00")),
        "origem": "WEBHOOK",
        "origem_arquivo": None,
    }
    pagamentos = [_traduzir_pagamento_webhook(p) for p in evento.get("pagamentos", [])]
    return cabecalho, pagamentos


@require_POST
def processar_webhook_agenda(request):
    """Consumidor da push subscription do Pub/Sub — correlaciona a UR do
    evento (SPEC03 §5.4) e persiste via upsert_agenda_ur (origem=WEBHOOK).
    Verificado por OIDC. Idempotente sob reentrega (at-least-once): o guard
    de webhook_inbox.processado_em evita refazer qualquer escrita."""
    if not verificar_push_oidc(request):
        return JsonResponse({"erro": "OIDC inválido"}, status=401)

    try:
        envelope = json.loads(request.body)
        dados = json.loads(base64.b64decode(envelope["message"]["data"]))
        webhook_inbox_id = dados["webhook_inbox_id"]
        financiador_id = dados["financiador_id"]
    except Exception:
        logger.exception("[Processor] Envelope do Pub/Sub push malformado")
        return JsonResponse({"erro": "envelope inválido"}, status=400)

    db = get_db(financiador_id)
    linhas_inbox = db.table("webhook_inbox").select("*").eq("id", webhook_inbox_id).execute()
    if not linhas_inbox.data:
        logger.error("[Processor] webhook_inbox_id=%s não encontrado (financiador=%s)", webhook_inbox_id, financiador_id)
        return JsonResponse({"erro": "webhook_inbox não encontrado"}, status=404)
    inbox = linhas_inbox.data[0]

    if inbox["processado_em"] is not None:
        return JsonResponse({}, status=204)

    payload = inbox["payload"]
    tipo_evento = payload.get("tipoEvento")
    evento = payload.get("evento")

    if tipo_evento != "agenda":
        logger.warning("[Processor] tipoEvento=%s fora do escopo deste consumidor, ignorando", tipo_evento)
        db.table("webhook_inbox").update({"processado_em": datetime.now(timezone.utc)}).eq("id", webhook_inbox_id).execute()
        return JsonResponse({}, status=204)

    candidatas = encontrar_consultas_candidatas(financiador_id, evento)
    cabecalho, pagamentos = _traduzir_evento_webhook(evento)

    if not candidatas:
        db.table("agenda_ur_orfa").insert({"payload": payload}).execute()
    else:
        upsert_agenda_ur(financiador_id, cabecalho, pagamentos)
        agora = datetime.now(timezone.utc)
        for consulta in candidatas:
            db.table("consulta_agenda_ur").insert({
                "consulta_id": consulta["id"],
                "entidade_registradora": cabecalho["entidade_registradora"],
                "cnpj_credenciadora": cabecalho["cnpj_credenciadora"],
                "documento_ufr": cabecalho["documento_ufr"],
                "documento_titular": cabecalho["documento_titular"],
                "codigo_arranjo": cabecalho["codigo_arranjo"],
                "data_liquidacao": cabecalho["data_liquidacao"],
                "origem": "WEBHOOK",
            }).execute()
            db.table("consulta_agenda").update({
                "qtd_urs_webhook": consulta["qtd_urs_webhook"] + 1,
                "ultima_ur_em": agora,
            }).eq("id", consulta["id"]).execute()

    db.table("webhook_inbox").update({"processado_em": datetime.now(timezone.utc)}).eq("id", webhook_inbox_id).execute()
    return JsonResponse({}, status=204)
```

- [ ] **Step 2: Registrar a rota em `apps/agenda/urls.py`**

Adicione à lista `urlpatterns`:

```python
    path("webhooks/agenda/processar", views.processar_webhook_agenda),
```

- [ ] **Step 3: Escrever `apps/agenda/tests/test_views_webhook_processor.py`**

```python
import base64
import json
from datetime import date, datetime, timezone

import pytest
from django.test import Client
from ulid import ULID

from apps.agenda import repository, views
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
URL = "/api/v1/webhooks/agenda/processar"
UFR_TESTE = "44444444000144"

CHAVE_UR_TESTE = {
    "data_liquidacao": "2026-09-25",
    "entidade_registradora": "22246686000196",
    "cnpj_credenciadora": "36216798000150",
    "documento_ufr": UFR_TESTE,
    "documento_titular": UFR_TESTE,
    "codigo_arranjo": "VCC",
}


def _push_envelope(webhook_inbox_id, financiador_id=FINANCIADOR_TESTE):
    dados = json.dumps({"webhook_inbox_id": webhook_inbox_id, "financiador_id": financiador_id}).encode()
    return json.dumps({
        "message": {"data": base64.b64encode(dados).decode(), "messageId": "msg-1", "publishTime": "2026-08-25T12:00:00Z"},
        "subscription": "projects/registradora-506000/subscriptions/agenda-webhook-inbox-sub",
    })


def _evento_agenda(**overrides):
    base = {
        "entidadeRegistradora": CHAVE_UR_TESTE["entidade_registradora"],
        "instituicaoCredenciadora": CHAVE_UR_TESTE["cnpj_credenciadora"],
        "documentoUsuarioFinalRecebedor": UFR_TESTE,
        "codigoArranjoPagamento": "VCC",
        "documentoTitular": UFR_TESTE,
        "dataLiquidacao": CHAVE_UR_TESTE["data_liquidacao"],
        "constituicao": "1",
        "valorConstituidoTotal": 1000.0,
        "valorTotalUR": 1000.0,
        "dataHoraUltimaAtualizacao": "2026-08-25T12:00:00.000Z",
        "pagamentos": [],
    }
    base.update(overrides)
    return base


def _payload(evento, tipo_evento="agenda"):
    return {"tipoEvento": tipo_evento, "dataHoraEvento": "2026-08-25T12:00:00.000Z", "evento": evento}


def _criar_webhook_inbox(payload, processado_em=None):
    webhook_id = str(ULID())
    get_db(FINANCIADOR_TESTE).table("webhook_inbox").insert({
        "id": webhook_id,
        "tipo_evento": payload["tipoEvento"],
        "data_hora_evento": datetime.fromisoformat(payload["dataHoraEvento"].replace("Z", "+00:00")),
        "payload": payload,
        "hash_dedupe": webhook_id,
        "processado_em": processado_em,
    }).execute()
    return webhook_id


def _criar_consulta(id_, **overrides):
    base = {
        "id": id_, "modo": "ONLINE", "status": "PARCIAL",
        "filtro_ufr": UFR_TESTE, "filtro_titular": None,
        "filtro_credenciadoras": ["99T"], "filtro_arranjos": ["99T"],
        "filtro_data_inicio": date(2026, 9, 1), "filtro_data_fim": date(2026, 9, 30),
        "base_autorizativa_tipo": "OPTIN", "base_autorizativa_id": "opt_1",
        "motivo": "TESTE-PROCESSOR", "ator": "teste@teste.com",
        "qtd_urs_sincrono": 0, "qtd_urs_webhook": 0,
    }
    base.update(overrides)
    get_db(FINANCIADOR_TESTE).table("consulta_agenda").insert(base).execute()
    return id_


def _limpar(webhook_inbox_id=None, consulta_ids=()):
    db = get_db(FINANCIADOR_TESTE)
    parcial = {"data_liquidacao": CHAVE_UR_TESTE["data_liquidacao"], "entidade_registradora": CHAVE_UR_TESTE["entidade_registradora"]}
    campos_parciais = ("data_liquidacao", "entidade_registradora")
    repository._com_filtros(db.table("agenda_ur_evento").delete(), parcial, campos_parciais).execute()
    repository._com_filtros(db.table("agenda_ur_pagamento").delete(), parcial, campos_parciais).execute()
    repository._com_filtros(db.table("agenda_ur").delete(), parcial, campos_parciais).execute()
    for consulta_id in consulta_ids:
        db.table("consulta_agenda_ur").delete().eq("consulta_id", consulta_id).execute()
        db.table("consulta_agenda").delete().eq("id", consulta_id).execute()
    db.table("agenda_ur_orfa").delete().eq("payload", None).execute()  # no-op seguro; órfãos são limpos por id abaixo
    if webhook_inbox_id:
        db.table("webhook_inbox").delete().eq("id", webhook_inbox_id).execute()


@pytest.fixture(autouse=True)
def _oidc_ok(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: True)


def test_processor_sem_oidc_retorna_401(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: False)
    response = Client().post(URL, data=_push_envelope("qualquer-id"), content_type="application/json")
    assert response.status_code == 401


def test_processor_envelope_pubsub_malformado_retorna_400():
    response = Client().post(URL, data="isto nao e json", content_type="text/plain")
    assert response.status_code == 400


def test_processor_webhook_inbox_nao_encontrado_retorna_404():
    response = Client().post(URL, data=_push_envelope("id-inexistente"), content_type="application/json")
    assert response.status_code == 404


def test_processor_ja_processado_e_idempotente():
    webhook_id = _criar_webhook_inbox(_payload(_evento_agenda()), processado_em=datetime(2026, 8, 25, 12, 5, tzinfo=timezone.utc))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204
    finally:
        _limpar(webhook_inbox_id=webhook_id)


def test_processor_tipo_evento_diferente_de_agenda_e_ignorado_mas_marcado_processado():
    webhook_id = _criar_webhook_inbox(_payload({"algumCampo": "valor"}, tipo_evento="contrato"))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        linha = get_db(FINANCIADOR_TESTE).table("webhook_inbox").select("*").eq("id", webhook_id).execute()
        assert linha.data[0]["processado_em"] is not None
    finally:
        _limpar(webhook_inbox_id=webhook_id)


def test_processor_sem_consulta_casada_vai_para_orfa():
    webhook_id = _criar_webhook_inbox(_payload(_evento_agenda()))
    db = get_db(FINANCIADOR_TESTE)
    antes = db.table("agenda_ur_orfa").select("id").execute().data
    ids_antes = {r["id"] for r in antes}
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        depois = db.table("agenda_ur_orfa").select("*").execute().data
        novas = [r for r in depois if r["id"] not in ids_antes]
        assert len(novas) == 1
        for r in novas:
            db.table("agenda_ur_orfa").delete().eq("id", r["id"]).execute()

        ur = repository._com_filtros(db.table("agenda_ur").select("*"), CHAVE_UR_TESTE, repository._CHAVE_UR).execute().data
        assert ur == []  # órfã não persiste em agenda_ur
    finally:
        _limpar(webhook_inbox_id=webhook_id)


def test_processor_com_consulta_casada_persiste_ur_e_vincula():
    consulta_id = _criar_consulta("proc-1")
    webhook_id = _criar_webhook_inbox(_payload(_evento_agenda()))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        db = get_db(FINANCIADOR_TESTE)
        ur = repository._com_filtros(db.table("agenda_ur").select("*"), CHAVE_UR_TESTE, repository._CHAVE_UR).execute().data
        assert len(ur) == 1
        assert ur[0]["origem"] == "WEBHOOK"

        vinculo = db.table("consulta_agenda_ur").select("*").eq("consulta_id", consulta_id).execute().data
        assert len(vinculo) == 1
        assert vinculo[0]["origem"] == "WEBHOOK"

        consulta = db.table("consulta_agenda").select("*").eq("id", consulta_id).execute().data[0]
        assert consulta["qtd_urs_webhook"] == 1
        assert consulta["ultima_ur_em"] is not None
    finally:
        _limpar(webhook_inbox_id=webhook_id, consulta_ids=[consulta_id])


def test_processor_casa_com_multiplas_consultas():
    consulta_a = _criar_consulta("proc-2a")
    consulta_b = _criar_consulta("proc-2b", motivo="OUTRO-MOTIVO")
    webhook_id = _criar_webhook_inbox(_payload(_evento_agenda()))
    try:
        response = Client().post(URL, data=_push_envelope(webhook_id), content_type="application/json")
        assert response.status_code == 204

        db = get_db(FINANCIADOR_TESTE)
        for consulta_id in (consulta_a, consulta_b):
            vinculo = db.table("consulta_agenda_ur").select("*").eq("consulta_id", consulta_id).execute().data
            assert len(vinculo) == 1

        ur = repository._com_filtros(db.table("agenda_ur").select("*"), CHAVE_UR_TESTE, repository._CHAVE_UR).execute().data
        assert len(ur) == 1  # persistido uma única vez, mesmo casando duas consultas
    finally:
        _limpar(webhook_inbox_id=webhook_id, consulta_ids=[consulta_a, consulta_b])
```

- [ ] **Step 4: Rodar a suíte**

Run: `pytest apps/agenda/tests/test_views_webhook_processor.py -v`
Expected: PASS em todos os testes.

- [ ] **Step 5: Commit**

```bash
git add apps/agenda/views.py apps/agenda/urls.py apps/agenda/tests/test_views_webhook_processor.py
git commit -m "feat: consumidor do webhook de agenda (correlacao + upsert, origem WEBHOOK)"
```

---

### Task 4: Job de completude (`varrer_completude`)

**Files:**
- Modify: `apps/agenda/views.py` (adiciona `varrer_completude`)
- Modify: `apps/agenda/urls.py` (registra a rota)
- Test: `apps/agenda/tests/test_views_completude.py`

**Interfaces:**
- Produces: view `varrer_completude` — endpoint interno disparado por Cloud Scheduler (mesma verificação OIDC de `shared.pubsub_auth.verificar_push_oidc`).

- [ ] **Step 1: Adicionar a `apps/agenda/views.py`**

```python
_TENANTS_JOBS_PERIODICOS = ["12345678000199"]  # lista fixa — design doc §14 ponto 3, não resolvido ainda
_QUIET_PERIOD_SEGUNDOS = 90
_HARD_TIMEOUT_SEGUNDOS = 15 * 60


@require_POST
def varrer_completude(request):
    """Job de completude (SPEC03 §5.5): sem sinal de fim do webhook, uma
    consulta ONLINE em PARCIAL vira COMPLETA após um quiet period sem
    novas URs, ou COMPLETA_COM_TIMEOUT após um hard timeout desde o início.
    Disparado por Cloud Scheduler, verificado pelo mesmo OIDC do consumidor
    Pub/Sub (design doc §8/Global Constraints deste plano)."""
    if not verificar_push_oidc(request):
        return JsonResponse({"erro": "OIDC inválido"}, status=401)

    agora = datetime.now(timezone.utc)
    resultado = {"completas": 0, "timeout": 0}

    for financiador_id in _TENANTS_JOBS_PERIODICOS:
        db = get_db(financiador_id)
        parciais = db.table("consulta_agenda").select("*").eq("status", "PARCIAL").execute().data

        for consulta in parciais:
            referencia = consulta["ultima_ur_em"] or consulta["iniciada_em"]
            idade_desde_ultima_ur = (agora - referencia).total_seconds()
            idade_total = (agora - consulta["iniciada_em"]).total_seconds()

            if idade_total > _HARD_TIMEOUT_SEGUNDOS:
                db.table("consulta_agenda").update({
                    "status": "COMPLETA_COM_TIMEOUT", "encerrada_em": agora,
                }).eq("id", consulta["id"]).execute()
                logger.warning("[Completude] consulta %s COMPLETA_COM_TIMEOUT (financiador=%s)", consulta["id"], financiador_id)
                resultado["timeout"] += 1
            elif idade_desde_ultima_ur > _QUIET_PERIOD_SEGUNDOS:
                db.table("consulta_agenda").update({
                    "status": "COMPLETA", "encerrada_em": agora,
                }).eq("id", consulta["id"]).execute()
                resultado["completas"] += 1

    return JsonResponse(resultado, status=200)
```

- [ ] **Step 2: Registrar a rota em `apps/agenda/urls.py`**

```python
    path("jobs/varrer-completude", views.varrer_completude),
```

- [ ] **Step 3: Escrever `apps/agenda/tests/test_views_completude.py`**

```python
from datetime import date, datetime, timedelta, timezone

import pytest
from django.test import Client

from apps.agenda import views
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
URL = "/api/v1/jobs/varrer-completude"
UFR_TESTE = "55555555000155"


def _consulta(id_, *, iniciada_em, ultima_ur_em=None, status="PARCIAL"):
    return {
        "id": id_, "modo": "ONLINE", "status": status,
        "filtro_ufr": UFR_TESTE, "filtro_credenciadoras": ["99T"], "filtro_arranjos": ["99T"],
        "filtro_data_inicio": date(2026, 9, 1), "filtro_data_fim": date(2026, 9, 30),
        "base_autorizativa_tipo": "OPTIN", "base_autorizativa_id": "opt_1",
        "motivo": "TESTE-COMPLETUDE", "ator": "teste@teste.com",
        "iniciada_em": iniciada_em, "ultima_ur_em": ultima_ur_em,
    }


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: True)
    monkeypatch.setattr(views, "_TENANTS_JOBS_PERIODICOS", [FINANCIADOR_TESTE])
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").delete().eq("filtro_ufr", UFR_TESTE).execute()
    yield
    db.table("consulta_agenda").delete().eq("filtro_ufr", UFR_TESTE).execute()


def test_varrer_completude_sem_oidc_retorna_401(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: False)
    response = Client().post(URL)
    assert response.status_code == 401


def test_fecha_como_completa_apos_quiet_period():
    agora = datetime.now(timezone.utc)
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta(
        "comp-1", iniciada_em=agora - timedelta(minutes=5), ultima_ur_em=agora - timedelta(seconds=100),
    )).execute()

    response = Client().post(URL)
    assert response.status_code == 200

    consulta = db.table("consulta_agenda").select("*").eq("id", "comp-1").execute().data[0]
    assert consulta["status"] == "COMPLETA"
    assert consulta["encerrada_em"] is not None


def test_fecha_como_timeout_apos_hard_timeout():
    agora = datetime.now(timezone.utc)
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta(
        "comp-2", iniciada_em=agora - timedelta(minutes=20), ultima_ur_em=agora - timedelta(seconds=10),
    )).execute()

    response = Client().post(URL)
    assert response.status_code == 200

    consulta = db.table("consulta_agenda").select("*").eq("id", "comp-2").execute().data[0]
    assert consulta["status"] == "COMPLETA_COM_TIMEOUT"


def test_nao_toca_consulta_ainda_dentro_do_quiet_period():
    agora = datetime.now(timezone.utc)
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta(
        "comp-3", iniciada_em=agora - timedelta(minutes=1), ultima_ur_em=agora - timedelta(seconds=10),
    )).execute()

    response = Client().post(URL)
    assert response.status_code == 200

    consulta = db.table("consulta_agenda").select("*").eq("id", "comp-3").execute().data[0]
    assert consulta["status"] == "PARCIAL"


def test_ignora_consulta_ja_completa():
    agora = datetime.now(timezone.utc)
    db = get_db(FINANCIADOR_TESTE)
    db.table("consulta_agenda").insert(_consulta(
        "comp-4", iniciada_em=agora - timedelta(minutes=20), status="COMPLETA",
    )).execute()

    response = Client().post(URL)
    assert response.status_code == 200

    consulta = db.table("consulta_agenda").select("*").eq("id", "comp-4").execute().data[0]
    assert consulta["status"] == "COMPLETA"
```

- [ ] **Step 4: Rodar a suíte**

Run: `pytest apps/agenda/tests/test_views_completude.py -v`
Expected: PASS em todos os testes.

- [ ] **Step 5: Commit**

```bash
git add apps/agenda/views.py apps/agenda/urls.py apps/agenda/tests/test_views_completude.py
git commit -m "feat: job de completude de consulta_agenda (quiet period + hard timeout, SPEC03 par.5.5)"
```

---

## Self-Review Notes

- **Spec coverage:** SPEC03 §5.1-§5.3 (comportamento, payload, requisitos do receptor) → Task 1. §5.4 (correlação) → Task 2. Consumidor + persistência com origem WEBHOOK → Task 3. §5.5 (completude) → Task 4.
- **Placeholder scan:** nenhum — todo step tem código executável completo.
- **Type consistency:** `encontrar_consultas_candidatas(financiador_id, evento) -> list[dict]` (Task 2) é exatamente o que a Task 3 consome. `upsert_agenda_ur(financiador_id, cabecalho, pagamentos)` usa os mesmos nomes de campo do Plano 05/06.
- **Duplicação deliberada, não corrigida:** `_traduzir_pagamento_webhook`/`_traduzir_evento_webhook` (Task 3, `apps/agenda/views.py`) são quase idênticas a `_traduzir_pagamento`/parte de `_traduzir_ur` de `services/cerc/client.py` (Plano 06) — mesma lógica, contextos de origem diferentes (uma UR fracionada por titulares vs. um evento já no nível de um único titular). Optou-se por duplicar em vez de extrair um módulo compartilhado para não reabrir um arquivo já revisado/mergeado do Plano 06 sem necessidade — considerar consolidar num módulo `apps/agenda/traducao_cerc.py` se um terceiro consumidor (Plano 08, arquivo AP005) precisar da mesma tradução de pagamento.
- **Risco aceito, documentado nas Global Constraints:** `qtd_urs_webhook` não é incrementado atomicamente — sob rajada real de webhooks concorrentes, pode subcontar. `consulta_agenda_ur` é a fonte de verdade real; a coluna é só conveniência.
- **Fora de escopo:** lista de tenants dos jobs periódicos ainda fixa (§14 ponto 3); consolidação da tradução de pagamento com o Plano 06 (ver acima); infraestrutura real do Pub/Sub/Cloud Scheduler (tópico, subscription, service account, agendamento do cron) — isso é provisionamento de GCP, fora do escopo de código deste plano, mesma convenção de "conexão real" tratada como dependência externa em planos anteriores.

**Next:** `2026-08-24-agenda-plan-08-ingestao-arquivo.md` (ingestão de arquivo AP005/AP005A/AP005B) — ainda não escrito.
