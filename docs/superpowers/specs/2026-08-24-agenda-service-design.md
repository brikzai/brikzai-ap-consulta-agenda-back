# Design — Agenda Service (SPEC 03 / CERC-AP005)

> **Status:** aguardando revisão do usuário
> **Repositório:** `ap-back-consulta-agenda`
> **Specs de origem:** `docs/specs/SPEC-03-consulta-de-agenda-ap005.md` (funcional/CERC). `docs/specs/SPEC-04-modelo-de-dados.md` fica só como referência de nomes de campo/regras de negócio — o DDL dela (schemas, domínios, particionamento) **não** é a convenção real da casa (ver §5).
> **Data:** 2026-08-24 (revisão 2 — realinhado à convenção já implementada em `ap-back-optin`/`ap-back-contratos`, ver §0)

## 0. Por que este documento foi revisado

A revisão 1 deste design assumia Django ORM, migrations, Celery e multi-tenancy híbrida (schema dedicado × schema+RLS) — nenhuma dessas decisões existia de fato na plataforma. Ao investigar os dois serviços irmãos já em construção (`C:\DEV\ap\ap-back-optin`, `C:\DEV\ap\ap-back-contratos`), a convenção real é:

- **Sem Django ORM.** `DATABASES = {}`; acesso via `shared/cloudsql_client.py` próprio.
- **Sem migration framework.** SQL versionado em `sql/schema/NN-*.sql`, aplicado por um script (`scripts/apply_schema.py`).
- **Multi-tenancy = um banco Cloud SQL inteiro por financiador**, não schema compartilhado nem RLS.
- **JWT validado localmente** (RS256, chave pública fixa) contra um **IdP corporativo** — nenhum serviço emite JWT para os outros.
- **Cada serviço autentica na CERC com credenciais próprias por tenant** — sem chamada entre serviços para obter token.
- **Sem Celery.** Deploy em **Cloud Run**; assíncrono via **Pub/Sub** (webhook) e **Cloud Scheduler** (jobs periódicos).
- **Function-based views**, sem DRF ViewSets.

Este documento adota essa convenção integralmente. A lógica de negócio (regras de agenda, correlação de webhook, parser de arquivo, catálogo de erros) não muda — só a forma de persistir e servir isso.

## 1. Contexto e objetivo

Este serviço (`agenda-service`) implementa a **consulta de agenda de recebíveis** (CERC-AP005), um dos três serviços do produto — irmão de `ap-back-optin` e `ap-back-contratos`, mesma squad, mesmo papel na cadeia CERC (Financiador):

| Serviço | Responsabilidade | Banco (Cloud SQL) |
|---|---|---|
| `optin-service` | Opt-in/opt-out | um banco **por financiador**, instância `app-db` (dev) |
| `agenda-service` (este) | Consulta de agenda batch/online, webhook de agenda, ingestão AP005/AP005A/AP005B | um banco **por financiador**, instância `app-db` (dev, banco `agenda`) |
| `contrato-service` | Registro de contratos e garantias (SPEC 02) | um banco **por financiador**, instância `contratos-db` (ainda pré-multi-tenant) |

Não há schema/cluster compartilhado entre serviços — cada um tem sua própria cópia local das tabelas de infraestrutura que precisa (`cerc_requisicao`, `webhook_inbox`, `dominio_arranjo`), como já é o caso em `optin`/`contrato`. Este documento cobre **apenas** o `agenda-service`. Dependências de outros serviços aparecem como **interfaces assumidas** (§14).

## 2. Escopo

**Dentro do escopo:**
- Consulta de agenda batch e online via `POST /v15/agenda/consultar` (cliente CERC)
- Recebimento e correlação do webhook `tipoEvento = agenda`
- Ingestão de arquivo AP005 / AP005A / AP005B
- API interna `/api/v1/agendas/*`
- Configuração self-service de política de modo (BATCH/ONLINE) por finalidade
- Compliance (trilha de auditoria, rate limit, relatório) e observabilidade do domínio de agenda

**Fora do escopo (dependências externas, ver §14):** emissão de JWT (IdP corporativo), opt-in/opt-out, registro de contratos.

**Fora da fase 1 (YAGNI, mesmo espírito do `contrato-service` §3/§8):** particionamento de tabela, domínios Postgres customizados, Row-Level Security. Nenhum dos três serviços irmãos precisou disso até agora — se o volume real de `agenda_ur` justificar depois, é uma migração deliberada, não uma antecipação especulativa.

## 3. Decisões transversais já fechadas

1. **Stack:** Python + Django, **sem ORM** (`DATABASES = {}`). Acesso a dados via `shared/cloudsql_client.py` — API estilo Supabase/PostgREST sobre SQLAlchemy + Cloud SQL Python Connector, **copiado** de `ap-back-optin` (não vira dependência de package compartilhado entre repos, mesma decisão já tomada para os outros dois serviços).
2. **Multi-tenancy:** um banco Cloud SQL **inteiro por financiador** (`financiador_id` = CNPJ, 14 dígitos). Config por tenant em `TENANT_{financiador_id}_CONFIG` (Secret Manager em produção, env var em dev), lida por `shared/tenant_config.py` (copiado de `ap-back-optin`, sem alterar).
3. **Autenticação:** JWT do IdP corporativo, validado localmente (RS256, chave pública fixa, sem JWKS) por `shared/jwt_auth.py` (copiado de `ap-back-optin`). Exige o claim `financiador_id` (CNPJ, 14 dígitos) — resolve o tenant e preenche `request.financiador_id` em toda view decorada com `@jwt_required`.
4. **CERC:** `agenda-service` autentica com **credenciais OAuth2 próprias**, por tenant (`services/cerc/token_provider.py`, copiado de `ap-back-optin`, chaveado por `financiador_id`). Nenhuma chamada a outro serviço para obter token — mesma decisão explícita já registrada no design do `contrato-service`.
5. **Deploy e assíncrono:** Cloud Run, sem Celery. Webhook → grava em `webhook_inbox` local, publica em tópico Pub/Sub, endpoint de push processa. Jobs periódicos (varredura de completude de consulta, polling de arquivo AP005, sincronização de `dominio_arranjo`) via **Cloud Scheduler** batendo em endpoints HTTP internos protegidos por OIDC.
6. **Views:** function-based, sem DRF ViewSets — API JSON pura consumida por um front separado (mesma decisão do `contrato-service`).
7. **Schema do banco:** SQL puro versionado em `sql/schema/NN-*.sql`, aplicado por `scripts/apply_schema.py`. Sem particionamento/domínios customizados na fase 1 (§2).

## 4. Estrutura de pastas

```
consulta-agenda/
├── manage.py
├── requirements.txt
├── Dockerfile
├── sql/schema/                        # 01-agenda-schema.sql = fase 1
├── scripts/apply_schema.py            # aplica um .sql no Cloud SQL real
├── config/                            # settings.py (DATABASES={}), urls.py, wsgi.py
├── apps/
│   └── agenda/
│       ├── views.py                   # API interna (§10) + webhook receptor CERC + push Pub/Sub
│       ├── urls.py
│       ├── validation.py              # A01-A10 (SPEC03 §10.1 + política de consulta)
│       ├── repository.py              # upsert de agenda_ur/agenda_ur_pagamento (regra de frescor)
│       ├── parser_ap005.py            # parser AP005/AP005A/AP005B
│       ├── correlacao.py              # casamento webhook <-> consulta (SPEC03 §5.4)
│       └── management/commands/       # varrer_completude, sincronizar_dominio_arranjo, importar_ap005
├── services/
│   └── cerc/
│       ├── token_provider.py          # copiado de ap-back-optin — OAuth2 client-credentials por tenant
│       └── client.py                  # consultar_agenda (online/batch)
└── shared/
    ├── cloudsql_client.py             # copiado de ap-back-optin
    ├── jwt_auth.py                    # copiado de ap-back-optin
    ├── tenant_config.py               # copiado de ap-back-optin
    ├── secrets.py                     # copiado de ap-back-optin
    └── pubsub_client.py                # publish helper (webhook inbox), mesmo molde do contrato-service
```

Decisões YAGNI explícitas (mesmo espírito dos dois irmãos):

- Sem interface/porta formal `CercAgendaGateway` — só existe o adapter REST hoje.
- Sem camada de domínio separada — regras locais cabem em `apps/agenda/validation.py`.
- Sem pasta `jobs/` própria — jobs são management commands, disparados por Cloud Scheduler.
- Sem parser AP005A/AP005B (arquivos segregados) nesta fase — entra quando o serviço de segregação for habilitado no portal do cliente (SPEC03 §6.5); o parser de AP005 já nasce tolerante ao layout dos três (mesma coluna-count detection), só o job de polling AP005A/AP005B fica pra depois.

## 5. Camada de dados — fase 1

Tabelas usadas por esta fase, nomes e campos vindos da SPEC03 (§4.3, §9) e da SPEC04 (só como referência de nomenclatura — não do DDL dela): `consulta_agenda`, `agenda_ur`, `agenda_ur_pagamento`, `consulta_agenda_ur`, `agenda_ur_orfa`, `agenda_ur_rejeitada`, `arquivo_agenda_processado`, `politica_consulta`, `cerc_requisicao`, `webhook_inbox`, `dominio_arranjo`.

**Fora da fase 1:** `indicador_consistencia_agenda` (entra junto com `tipoAvaliacao`, não usado ainda pela API interna fase 1).

Tipos monetários: `NUMERIC(18,2)` no Postgres, `decimal.Decimal` em Python. **Proibido `float`/`double`** (mesmo requisito do `contrato-service`, verificado por teste).

IDs técnicos: `TEXT`, gerados como ULID na aplicação (`python-ulid`) — mesma convenção dos dois serviços irmãos.

`agenda_ur`/`agenda_ur_pagamento` usam a **chave natural da UR** como chave primária composta (sem `id` técnico) — mesma razão da SPEC04 §11.1 (a chave natural já é a identidade real do dado, e evita uma coluna extra em uma tabela que pode crescer muito), só que sem particionamento (§2) — se o volume justificar depois, particionar é uma migração isolada, não uma decisão da fase 1.

## 6. Roteamento de tenant

`@jwt_required` (de `shared/jwt_auth.py`) resolve `request.financiador_id` a partir do JWT. Toda view que acessa dados chama `get_db(request.financiador_id)` (de `shared/cloudsql_client.py`) e, quando precisa falar com a CERC, `get_cerc_token(request.financiador_id)` (de `services/cerc/token_provider.py`). Não há middleware de schema/RLS — o isolamento é físico (um banco por tenant), então uma query mal escrita não pode vazar dado de outro financiador por definição (não existe "outro financiador" visível na mesma conexão).

Jobs disparados por Cloud Scheduler (varredura de completude, polling de arquivo) rodam **por tenant**: o endpoint interno itera a lista de tenants configurados (§14, ponto em aberto — como essa lista é obtida) e chama `get_db(financiador_id)` para cada um.

## 7. Política de consulta (self-service, fail-closed)

Tabela `politica_consulta`:

```sql
CREATE TABLE politica_consulta (
  id                 TEXT PRIMARY KEY,
  motivo             TEXT NOT NULL,
  modos_permitidos   TEXT[] NOT NULL,
  ativo              BOOLEAN NOT NULL DEFAULT true,
  criado_em          TIMESTAMPTZ NOT NULL DEFAULT now(),
  atualizado_em      TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (motivo)
);
```

Sem coluna `cnpj_financiador` — o isolamento já é o banco inteiro (§6), então "a política daquele financiador" é só "a linha nesse banco". Validação de `modos_permitidos <@ ARRAY['BATCH','ONLINE']` vira checagem em `apps/agenda/validation.py` (não `CHECK` de banco — mesmo estilo dos irmãos, que preferem `TEXT` simples + validação de aplicação a `CHECK` elaborado, já que o domínio muda por decisão de produto, não por regra imutável do banco).

Regras:
- **Fail-closed:** sem política ativa para `motivo` → `403 POLITICA_NAO_CONFIGURADA`, sem chamar a CERC. Nova regra local `A10`, ao lado de `A01`–`A09` (SPEC03 §10.1).
- O campo `modo` continua vindo do chamador (compatibilidade com SPEC03 §7.1), mas é **validado** contra `modos_permitidos` da política daquele `motivo`.
- Self-service: cada financiador só vê/edita a própria política — automático, dado que cada um tem seu próprio banco.
- Endpoints: `GET/PUT/DELETE /api/v1/config/politicas-consulta`.

## 8. Integração com a CERC

**Cliente de consulta (`POST /v15/agenda/consultar`, `services/cerc/client.py`):**
1. Roda `A01`–`A10` localmente antes de qualquer chamada externa (`apps/agenda/validation.py`).
2. `get_cerc_token(financiador_id)` — token cacheado em memória por processo, renovação a 80% de `expires_in`, single-flight via lock (mesmo padrão de `services/cerc/token_provider.py` do optin). Em `401`, invalida e repete uma única vez.
3. Toda chamada grava uma linha em `cerc_requisicao` **antes** de interpretar a resposta (mesmo padrão dos irmãos).
4. **Batch** (`online=false`/omitido): chama, persiste com `origem='SINCRONO'` (upsert §9 abaixo), devolve `200` consolidado.
5. **Online** (`online=true`): chama, persiste o retorno síncrono (`origem='SINCRONO'`), cria `consulta_agenda` em `PARCIAL`, devolve `202` com `consultaId` imediatamente — nunca espera o webhook.
6. Mapeamento de erros CERC (SPEC03 §10): `105001` → sucesso vazio (`200`, lista vazia); `105003`/`105999`/`105998` → retentável com backoff; `105802` → não deveria ocorrer (bloqueado antes por `A07`), e se ocorrer é alerta crítico; demais códigos de validação → `422` local.

**Receptor de webhook (`tipoEvento=agenda`, `POST /api/v1/webhooks/agenda`):**
- Handler grava o payload bruto em `webhook_inbox` (com `hash_dedupe`) e responde `2xx` em <200ms — nenhuma lógica de negócio na rota. Registrado diretamente na CERC, não passa por outro serviço.
- Publica em um tópico Pub/Sub (`agenda-webhook-inbox`); se o publish falhar, um job de varredura (Cloud Scheduler) recupera por `processado_em IS NULL` — mesmo padrão do `contrato-service`.
- Push subscription bate num endpoint próprio (verificado por OIDC) que roda a **correlação** (SPEC03 §5.4, `apps/agenda/correlacao.py`): casa `(instituicaoCredenciadora, codigoArranjoPagamento, documentoUsuarioFinalRecebedor, documentoTitular, dataLiquidacao)` contra `consulta_agenda` em `PARCIAL`/`ONLINE`, tratando `99T` como universo e a janela de datas como intervalo.
  - 0 casamentos → `agenda_ur_orfa` (nunca descarta).
  - ≥1 casamento → grava em `consulta_agenda_ur` para cada consulta casada; upsert em `agenda_ur`/`agenda_ur_pagamento` com `origem='WEBHOOK'`.
  - Deduplicação por `hash_dedupe` (`UNIQUE` na tabela).

**Critério de completude (sem sinal de fim, SPEC03 §5.5):** job `varrer_completude` (Cloud Scheduler, a cada ~30s, por tenant — §6) varre `consulta_agenda WHERE status='PARCIAL'`:
- `now() - ultima_ur_em > 90s` → `COMPLETA`
- `now() - iniciada_em > 15min` → `COMPLETA_COM_TIMEOUT` + alerta

**Rate limit (`A08`, 10 consultas online/UFR/dia):** contagem em `consulta_agenda` (índice em `filtro_ufr, iniciada_em, modo`). "Dia" é o dia calendário em `America/Sao_Paulo` — mesmo fuso já usado em todo o resto da aplicação (`TIME_ZONE` do settings, §3).

**Regra de upsert de `agenda_ur` (frescor):**

```python
def precedencia_origem(origem: str) -> int:
    return {"WEBHOOK": 3, "SINCRONO": 2, "ARQUIVO": 1}[origem]
```

Só sobrescreve se `data_hora_ultima_atualizacao` do dado novo for mais recente, ou empatado com origem de maior precedência — implementado em Python em `apps/agenda/repository.py` (lê a linha existente via `get_db(financiador_id).table("agenda_ur").select()...`, compara, decide `update` ou não), já que `cloudsql_client.py` não tem um `upsert()` condicional embutido (só `insert`/`update`/`delete` simples — ver §11 riscos).

## 9. Ingestão de arquivo (AP005 / AP005A / AP005B)

- **Chegada:** job `importar_ap005` (Cloud Scheduler, por tenant) faz polling em SFTP/Bucket/Connect:Direct por `CERC-AP005[A|B]_{ident_ic}_{data_req}_{seq}_ret.csv[.gz]` em `/informacoes_agendas/saida/`. *(Conexão/credenciais reais são dependência de infra a confirmar — não bloqueia este design.)*
- **Idempotência:** checa `arquivo_agenda_processado` por `(tipo_leiaute, ident_ic, data_req, seq)` antes de processar.
- **Streaming:** AP005A/AP005B vêm em `.gz`, descompactados via streaming — nunca materializa o arquivo inteiro em memória.
- **Detecção de layout** pela contagem de colunas: completo (16 col.) vs. reduzido "sem agenda" (3 col., resultado válido) vs. com/sem coluna 12.16.
- **Origem** (`AP005`/`AP005A`/`AP005B`) vem do nome do arquivo — um único parser (`apps/agenda/parser_ap005.py`) para os três.
- `tipoInformacaoPagamento` aceito `1`–`8` em arquivo e webhook; fora disso → `agenda_ur_rejeitada`, nunca exceção não tratada.
- Linha inválida → `agenda_ur_rejeitada` com motivo; arquivo continua. Alerta se rejeição > 0,5%.
- **Carga em massa:** sem `COPY`/tabela temporária (isso pressupõe SQL direto fora do query-builder do `cloudsql_client.py`) — para a fase 1, inserir em lotes (`insert()` com lista de dicts, que `QueryBuilder._exec_insert` já processa em uma única transação) após deduplicar em Python por chave natural (mantendo o registro de maior `data_hora_ultima_atualizacao`). Reavaliar se o volume real de linhas por arquivo tornar isso lento — não otimizar antes de medir.

## 10. API interna (`/api/v1/agendas/*`, `/api/v1/config/*`, `/api/v1/compliance/*`)

Function-based views (`apps/agenda/views.py`), sem DRF ViewSets. Todas exigem `@jwt_required` (exceto `health`).

| Endpoint | Descrição |
|---|---|
| `POST /api/v1/agendas/consultas` | Dispara consulta batch/online (valida `A01`–`A10`, chama CERC) |
| `GET /api/v1/agendas/consultas/{id}` | Status consolidado (`PARCIAL`/`COMPLETA`/`COMPLETA_COM_TIMEOUT`), contagem por origem |
| `GET /api/v1/agendas/urs` | Repositório consolidado, filtros + paginação por cursor (`limit ≤ 1000`) |
| `GET /api/v1/agendas/urs/posicao` | Visão agregada de crédito por UFR/janela, fumaça sempre segregada |
| `GET/PUT/DELETE /api/v1/config/politicas-consulta` | Política self-service de modo por finalidade |
| `GET /api/v1/compliance/relatorio` | Relatório de consultas por período/UFR/ator (síncrono/paginado inicialmente) |
| `POST /api/v1/webhooks/agenda` | Receptor do webhook CERC (§8) |

## 11. Compliance e auditoria

- `ator` (de `request.jwt_claims`) e `origem_ip` preenchidos no momento da consulta, gravados em `consulta_agenda` — retenção de 5 anos, sem expurgo.
- Relatório exportável evolui para job assíncrono com link de download se o volume exigir — não construído de antemão sem necessidade comprovada.

## 12. Observabilidade

- Métricas: contadores/histogramas equivalentes aos da SPEC03 §11 (`agenda_consultas_total`, `agenda_cerc_latency_seconds`, `agenda_webhook_orfas_total`, `agenda_ur_frescor_horas`) — mecanismo concreto (Cloud Monitoring custom metrics vs. `django-prometheus`) a decidir no plano de observabilidade, não nesta fase.
- Alertas na configuração de monitoring do GCP, fora do código — mesmo padrão dos irmãos.
- Logs estruturados (mesmo `LOGGING` do settings dos outros dois serviços) com `correlacao_id`; documentos sempre mascarados no log.

## 13. Testes

- `pytest` + `pytest-django` (para o test client de views; sem `pytest-django`'s DB fixtures, já que não há Django ORM/DATABASES).
- **Unitários:** regras `A01`–`A10`; parser AP005 (todas as variações de layout); branching de `tipoInformacaoPagamento`; upsert de frescor/precedência (`apps/agenda/repository.py`); algoritmo de correlação (`99T`, casamento múltiplo); `token_provider` (renovação 80%, single-flight).
- **Integração:** cenários equivalentes a IT-01–IT-18 da SPEC03 §12.2, contra o banco `agenda` real (dev, `app-db` — ver §14), não testcontainers (mesma prática dos irmãos: sem Docker nesta máquina, conecta direto no Cloud SQL real de dev). Stub da API CERC via `respx` (mesma lib do `contrato-service`).
- **Carga:** receptor de webhook (k6/locust, alvo a confirmar — Cloud Run escala diferente de um worker Celery dedicado); ingestor com arquivo grande validando memória constante.

## 14. Interfaces assumidas de outros serviços / pontos em aberto

1. **JWT:** emitido pelo IdP corporativo (não por `optin-service`). `IAM_JWT_PUBLIC_KEY`/`IAM_JWT_ISSUER` — mesmas env vars que os outros dois serviços já usam.
2. **Verificação de opt-in ativo (base autorizativa, `A07`):** `optin-service` guarda essa informação no **seu próprio banco**, fisicamente separado do banco do `agenda-service`. **Não existe hoje** um endpoint HTTP do `optin-service` para consultar isso — o único endpoint existente lá é `GET /health`. Este é um gap real, não uma suposição resolvida: sem esse endpoint, `A07` não pode ser verificado antes de chamar a CERC. Ação: confirmar com quem mantém `optin-service` se/quando esse endpoint sai, e qual o contrato (`financiador_id` + UFR + credenciadoras + arranjos + janela → ativo/inativo).
3. **Lista de tenants para jobs periódicos (§6):** os jobs disparados por Cloud Scheduler precisam saber quais `financiador_id` existem, para iterar um por um. Nenhum serviço irmão resolveu isso ainda publicamente (ver `2026-08-24-multitenancy-design.md §9` do `optin-service`: "provisionamento de tenants... será gerenciado por um front apartado"). Até esse front existir, a lista de tenants de dev/teste é fixa (`12345678000199`, mesmo tenant fixo que os outros dois serviços usam).
4. **Conexão real (credenciais SFTP/Bucket/Connect:Direct)** para chegada do arquivo AP005 — a definir.

## 15. Riscos conhecidos

1. **Upsert condicional sem suporte nativo no `cloudsql_client.py`.** A regra de frescor (§8) precisa de um `SELECT` + comparação em Python antes do `UPDATE`/`INSERT` — não é atômico como o `ON CONFLICT ... WHERE` do Postgres seria. Em alta concorrência (duas origens atualizando a mesma UR quase simultaneamente) existe uma janela de corrida teórica. Mitigação futura, se isso vier a importar na prática: um método `upsert()` dedicado em `cloudsql_client.py` usando SQL bruto (`text()`) para essa tabela específica, mantendo o restante do código no query-builder.
2. **Sem particionamento.** Se `agenda_ur` crescer para a casa dos milhões/bilhões de linhas como a SPEC04 projeta, será necessário revisitar essa decisão — aceito conscientemente para a fase 1 (§2), mesmo risco que os dois serviços irmãos já carregam.
3. **Correlação consulta↔webhook é heurística** (SPEC03 §5.4) — se a CERC expuser um id de consulta no futuro, simplifica.
4. **Gap do item 2 do §14** é bloqueante para produção (não para dev): sem o endpoint de opt-in do `optin-service`, `A07` não pode ser verificado de verdade contra dado real — só localmente com um mock, até esse endpoint existir.

## 16. Ordem de implementação recomendada (planos)

Mesma granularidade da série do `contrato-service` (~10 planos pequenos, cada um independentemente revisável/testável):

1. **Scaffold** — projeto Django sem ORM, endpoint de health.
2. **Schema** — `sql/schema/01-agenda-schema.sql` (fase 1, §5) aplicado no Cloud SQL real (banco `agenda`, já provisionado em `app-db`).
3. **Camada de dados compartilhada** — `shared/cloudsql_client.py`, `shared/secrets.py`, `shared/tenant_config.py` (copiados de `ap-back-optin`).
4. **Autenticação** — `shared/jwt_auth.py` + `services/cerc/token_provider.py` (copiados de `ap-back-optin`, adaptados ao domínio CERC de agenda).
5. **Repositório de upsert de `agenda_ur`** (regra de frescor, §8).
6. **Cliente CERC de consulta** (batch primeiro, depois online) + validações `A01`–`A10` + política de consulta (§7).
7. **Webhook + correlação + Pub/Sub + job de completude.**
8. **Ingestão de arquivo AP005.**
9. **API interna completa** (endpoints restantes de §10) + compliance.
10. **Observabilidade + testes de carga.**

Planos 3+ são escritos **depois** que o plano anterior estiver implementado e revisado — não faz sentido detalhar o plano 5 antes de saber que o 3 e o 4 realmente funcionaram (mesma prática já adotada nos dois serviços irmãos).
