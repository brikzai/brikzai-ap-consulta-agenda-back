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

- **Chegada — decisão de escopo do Plano 08 (confirmada com o usuário):** sem poller real nesta fase. O Plano 08 construiu só o parser e a função de ingestão (`apps/agenda/importar_ap005.py`), expostos por um endpoint HTTP interno (`POST /api/v1/jobs/importar-ap005/{financiador_id}`, protegido pelo mesmo OIDC dos outros jobs internos) que recebe o arquivo já em mãos — nome no header `X-Nome-Arquivo`, conteúdo lido em streaming do corpo bruto da requisição. A chegada original imaginada aqui (job `importar_ap005` via Cloud Scheduler, por tenant, fazendo polling em SFTP/Bucket/Connect:Direct por `CERC-AP005[A|B]_{ident_ic}_{data_req}_{seq}_ret.csv[.gz]` em `/informacoes_agendas/saida/`) permanece **não construída** — um plano futuro liga o endpoint a Cloud Scheduler + o canal real, quando a credencial existir (dependência de infra ainda a confirmar, §14 item 4).
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

**Atenção de segurança para `GET /api/v1/agendas/urs`, `GET /api/v1/agendas/urs/posicao` e `GET /api/v1/compliance/relatorio` (achado na revisão final do Plano 03):** `QueryBuilder` (`shared/cloudsql_client.py`) monta nome de tabela/coluna por interpolação de string, não por bind parameter — só os *valores* passam por bind. A revisão final do Plano 03 adicionou uma validação de identificador (`_validate_identifier`, regex `^[A-Za-z_][A-Za-z0-9_]*$`) em `table()`, `.eq()`/`.gte()`/`.lte()`/`.order()` e nas chaves de dict de `insert()`/`update()` — isso fecha a classe de SQL injection (um valor com `;`/aspas não passa mais), mas **não substitui** a validação de negócio: um identificador válido ainda pode ser uma coluna que a view não deveria expor (ex. `documento_titular` de outro contexto). Essas três views são as primeiras a mapear um parâmetro de query string do usuário para um nome de coluna — continuar validando contra uma lista fixa de colunas permitidas antes de chamar `.eq()`/`.order()`, mesmo com a guarda de identificador já em vigor. `.select(fields=...)` continua sem nenhuma guarda (aceita `"*"` ou projeções compostas) — nunca repassar um valor de query string direto para esse parâmetro.

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
5. **`referenciaExterna` (achado na análise de integração com `ap-front`, `ScheduleView.tsx`):** é um campo que a CERC entrega (referência externa da UR), mas ainda não está mapeado para nenhuma coluna do schema atual (`agenda_ur`/`agenda_ur_pagamento` não têm equivalente) nem documentado na SPEC03 com precisão sobre origem/formato. Campo aberto — entender exatamente em qual payload da CERC ele aparece e se precisa de coluna própria (ou se já está coberto pelo JSONB bruto de `webhook_inbox`/`cerc_requisicao`) antes de qualquer plano que o exponha na API.

## 15. Riscos conhecidos

1. **Upsert condicional — decisão tomada no Plano 05: opção (b), `SELECT` + comparação em Python.** Esta seção dizia que `cloudsql_client.py` não tinha upsert nativo, o que motivaria um `SELECT` + comparação em Python antes do `UPDATE`/`INSERT` (não atômico). Isso não é mais verdade: `ap-back-contratos/shared/cloudsql_client.py` já ganhou um `.upsert(data, on_conflict=...)` que emite `INSERT ... ON CONFLICT (...) DO UPDATE SET ... RETURNING *` — **mas sem cláusula `WHERE`**, ou seja, é um upsert incondicional (a última escrita sempre vence), não a regra de precedência de frescor do §8 (`WEBHOOK > SINCRONO > ARQUIVO`, e "mais recente" tem que vencer mesmo com origem de menor precedência). O Plano 05 (`apps/agenda/repository.py`) decidiu explicitamente por (b) — manter o `SELECT` + comparação em Python do design original, sem estender `shared/cloudsql_client.py` — mais simples de ler, aceita a janela de corrida teórica, e mantém esse arquivo (já revisado/mergeado no Plano 03) intocado.
   - **Consequência confirmada na revisão do Plano 05 (achado Important, parked):** como cada `.execute()` de `QueryBuilder` abre sua própria transação (`shared/cloudsql_client.py`, `engine.begin()` por chamada), `upsert_agenda_ur` não tem boundary transacional único ao longo de suas várias escritas (cabecalho, eventos de captura/bloqueio/disponibilização, cada pagamento, limpeza de pagamentos obsoletos) — uma falha no meio do lote deixa escritas parciais sem rollback. Blast radius limitado (uma UR, autocurativo no próximo lote bem-sucedido), não escolhido como bloqueante desta task porque é consequência direta desta mesma decisão (b) e do Global Constraint de não tocar `shared/cloudsql_client.py`. Carregar para os Planos 06/07/08 (todos chamam só `upsert_agenda_ur`, todos herdam esse gap) — se o volume real de falhas parciais se mostrar relevante em produção, a solução é uma API de transação cross-statement no cliente compartilhado (Plano 03) ou uma reformulação da sequência de escrita, não um ajuste pontual num desses planos. Refinamento da revisão final: o argumento "autocurativo no próximo lote" vale para o *estado* (a próxima escrita bem-sucedida sobrescreve o estado desatualizado), mas não vale para o *histórico* — se o `UPDATE` de `agenda_ur` for bem-sucedido mas o `INSERT` do evento correspondente falhar, o próximo lote compara contra o cabeçalho já atualizado e não gera evento nenhum: esse evento específico se perde permanentemente numa tabela append-only, sem caminho de backfill. É essa assimetria (histórico perdido, não o estado inconsistente) que os Planos 06/07/08 devem levar em conta.
2. **Sem particionamento.** Se `agenda_ur` crescer para a casa dos milhões/bilhões de linhas como a SPEC04 projeta, será necessário revisitar essa decisão — aceito conscientemente para a fase 1 (§2), mesmo risco que os dois serviços irmãos já carregam. `agenda_ur_evento` (Plano 05) herda a mesma decisão e cresce mais rápido que `agenda_ur` (múltiplas linhas por mutação de uma única UR) — candidata a ser a primeira tabela a precisar de particionamento/retenção quando essa decisão for revisitada.
3. **Correlação consulta↔webhook é heurística** (SPEC03 §5.4) — se a CERC expuser um id de consulta no futuro, simplifica.
4. **Gap do item 2 do §14** é bloqueante para produção (não para dev): sem o endpoint de opt-in do `optin-service`, `A07` não pode ser verificado de verdade contra dado real — só localmente com um mock, até esse endpoint existir.
5. **Descompasso de vocabulário entre duas colunas do mesmo conceito** (achado na revisão do Plano 02): `agenda_ur.origem_arquivo` guarda `AP005`/`AP005A`/`AP005B` (sem prefixo), enquanto `arquivo_agenda_processado.tipo_leiaute` vai guardar o `Tipo_Leiaute` do nome do arquivo, que a SPEC03 §6.1 fixa como `CERC-AP005` (com prefixo). O Plano 08 (ingestão de arquivo) precisa decidir uma convenção única (ambos com prefixo, ou ambos sem) antes de escrever o parser — não é um problema de schema, é uma decisão de normalização a registrar ali.
   - **Decisão tomada no Plano 08:** `agenda_ur.origem_arquivo` passa a gravar o MESMO valor com prefixo que `arquivo_agenda_processado.tipo_leiaute` (`CERC-AP005`/`CERC-AP005A`/`CERC-AP005B`), sem conversão (`apps/agenda/parser_ap005.py`, `parse_nome_arquivo`) — fecha o descompasso sem exigir migração, já que nenhum plano anterior tinha gravado dado real nessa coluna.
6. **`agenda_ur_pagamento.indicador_efeitos_contrato NOT NULL DEFAULT ''`** é a escolha certa (permite ser alvo de `ON CONFLICT`, SPEC04 §0 já apontava isso), mas exige que o parser do Plano 08 traduza "campo ausente" (SPEC03 §4.4: obrigatório só quando é efeito de contrato) para `''`, nunca para `None`/`NULL` — passar `None` vai violar o `NOT NULL`. Registrar isso explicitamente no Plano 08 quando ele for escrito, não descobrir só quando o parser falhar.
   - **Decisão tomada no Plano 08:** `parser_ap005._traduzir_pagamento` traduz o campo ausente/vazio (coluna 12.14) para `""` (`campo or ""`), nunca para `None` — nunca viola o `NOT NULL`.
7. **Sem estratégia para linhas obsoletas de `agenda_ur_pagamento`** (achado na revisão do Plano 02): se uma UR for recebida de novo com um efeito removido (ex.: um ônus que deixou de existir), o upsert por linha deixa a linha antiga do efeito removido no banco para sempre — ela nunca é escrita de novo, então nunca é atualizada, mas também nunca é removida. `atualizado_em` (já presente no schema) permite a correção padrão (`DELETE` das linhas daquela UR com `atualizado_em` anterior ao timestamp do lote atual, um `DELETE` com prefixo de PK) — o Plano 05 (repositório de upsert) precisa implementar isso, não é automático.

8. **Mascaramento de documento em log é parcial** (achado na revisão final do Plano 03, corrigido em parte). `hide_parameters=True` no engine suprime o bloco `[parameters: ...]` do SQLAlchemy nas mensagens de erro, mas **não suprime** o detalhe nativo do Postgres (`'D': 'Failing row contains (...)'`) que aparece dentro da própria exceção do driver — um erro de constraint em `agenda_ur`/`agenda_ur_pagamento` ainda pode expor CPF/CNPJ em texto claro no log via esse caminho. `§12` ("documentos sempre mascarados no log") precisa de uma decisão explícita em Plano 05/07 sobre logar um resumo redigido em vez de `logger.exception(...)` cru — não resolvido pelo engine sozinho.
9. **`_deserialize_row` tem um alvo concreto no schema fase 1** (achado na revisão final do Plano 03): a heurística que reinterpreta qualquer string começando com `{`/`[` como JSON vai capturar `agenda_ur_rejeitada.conteudo` sempre que a linha rejeitada originar de um payload JSON (webhook) — o conteúdo *bruto* rejeitado (que existe justamente para auditoria/forense, SPEC03 catálogo de erros) voltaria como `dict` em vez do texto original byte-a-byte. Nomear como cuidado explícito para os Planos 07/08 (quem grava `agenda_ur_rejeitada`), não descobrir isso só quando uma investigação de rejeição precisar do payload exato.
10. **Nome amigável de credenciadora — tabela nova, decidido criar** (achado na análise de integração com `ap-front`). O front precisa exibir nome comercial (`Cielo`/`Rede`/`Stone`/`GetNet`) a partir do `cnpj_credenciadora` que o schema guarda hoje. Não existe tabela para isso (`cerc.participante_slc` é outra coisa — domicílio bancário/SLC). Decisão: criar uma tabela sincronizável de credenciadora (`cnpj → nome fantasia`), no mesmo espírito de `dominio_arranjo`/`participante_slc` — nunca hardcode no código da aplicação. Definir em qual plano (provável: junto do Plano 06, cliente CERC, ou como parte da API do Plano 09) e como ela é populada/atualizada (não há fonte confirmada ainda — a definir com a CERC ou fonte pública de credenciadoras SLC).
11. **`dominio_arranjo` sem seed real e sem estrutura para bandeira/modalidade — essencial, bloqueante para exibir dado real** (achado na análise de integração com `ap-front`). A tabela existe (`sql/schema/01-agenda-schema.sql`, coluna única `descricao TEXT`) mas está vazia — nenhum código real da CERC (`VCC` = Visa Crédito, `VCD` = Visa Débito, etc.) foi semeado. Confirmado com o usuário: a CERC entrega bandeira+modalidade codificados em `codigoArranjoPagamento`, então isso **precisa** ser resolvido, não é opcional. Duas decisões pendentes antes do Plano 06/09: (a) onde obter a lista completa e oficial de códigos da CERC para seed inicial e sincronização contínua; (b) se `descricao` (texto livre, ex. "Visa Crédito") basta, ou se a tabela precisa de colunas estruturadas (`bandeira`, `modalidade`) para o front não ter que fazer parsing de string.
12. **Sem histórico de eventos por UR — essencial, lacuna de modelagem** (achado na análise de integração com `ap-front`, confirmado como requisito core do produto, não só UI). O schema atual (`agenda_ur`/`agenda_ur_pagamento`) guarda só o **estado atual** da UR, sem trilha de "Captura → Bloqueio/Disponibilização → Liquidação" ao longo do tempo — não há equivalente a `contrato.contrato_evento`. Se a timeline é essencial ao produto (confirmado), isso é uma tabela nova a desenhar (candidato: `agenda.agenda_ur_evento`, append-only, particionada por tempo como `contrato_evento`) **antes ou junto do Plano 05** (repositório de upsert), já que o repositório de upsert é quem vai precisar gravar esses eventos a cada mudança de estado, não só fazer upsert do estado final. Decidir o gatilho de cada evento (o que conta como "Bloqueio", "Disponibilização", "Liquidação" em termos dos campos que a CERC realmente entrega) é pré-requisito de design, não de implementação.
   - **Decisão tomada no Plano 05:** tabela `agenda_ur_evento` criada (sem particionamento nesta fase, mesma decisão do risco 2), com gatilhos: `CAPTURA` no primeiro upsert de uma UR (chave nunca vista); `BLOQUEIO` quando `valor_bloqueado` aumenta entre um upsert e o anterior; `DISPONIBILIZACAO` quando `valor_livre` aumenta; `LIQUIDACAO` por linha de `agenda_ur_pagamento` que recebe `data_liquidacao_efetiva` pela primeira vez (evento carrega `tipo_informacao_pagamento`/`indicador_efeitos_contrato` do pagamento de origem, colunas adicionadas na revisão final do Plano 05 pra distinguir múltiplas liquidações da mesma UR). `valor` não é uniforme entre tipos — é a grandeza monetária relevante de cada evento (saldo resultante para `BLOQUEIO`/`DISPONIBILIZACAO`, valor total para `CAPTURA`, valor do efeito para `LIQUIDACAO`), não um delta.
   - **Lacunas conhecidas e aceitas (achado na revisão final do Plano 05, carregar para Planos 06/07/08):** (a) liberação de bloqueio (`valor_bloqueado` diminuindo) não gera evento — só aumento gera `BLOQUEIO`; (b) uma UR que chega pela primeira vez já bloqueada só gera `CAPTURA`, pulando direto pra `Liquidação` na timeline (relevante para o Plano 08, que ingere arquivo com o estado consolidado do dia); (c) um lote descartado por ser mais antigo (`sobrescrito: False`) não contribui nenhum evento — a granularidade da timeline depende da ordem de chegada dos lotes. Nenhuma dessas é um bug — são consequências do estado atualmente disponível (a CERC não expõe uma "razão da mudança" explícita) — mas precisam estar visíveis para quem desenhar a API de timeline (Plano 09) e para quem avaliar se a UX do front precisa de mais granularidade do que isso entrega.
13. **`apps/agenda/repository.upsert_agenda_ur` não é otimizado para volume em lote (achado na revisão final do Plano 05).** A função faz de 1 a ~2 round trips ao banco por linha de pagamento (mais eventos, mais a limpeza de obsoletos), cada um em sua própria transação — aceitável para os volumes de consulta síncrona/webhook (Planos 06/07, uma UR por vez), mas o Plano 08 (ingestão de arquivo AP005, SPEC04 projeta volume na casa dos milhões de linhas/dia) pode achar isso inviável em throughput. Esta função foi declarada o único caminho de escrita de UR para os Planos 06/07/08 — o Plano 08 precisa decidir explicitamente entre: (a) usar `upsert_agenda_ur` mesmo assim, se o volume real permitir; (b) estender esse repositório com um caminho de escrita em lote (ex.: usando `insert()` com lista de dicts, já suportado por `shared/cloudsql_client.py`); (c) um caminho de escrita alternativo específico do Plano 08. Não decidir agora — só não assumir que `upsert_agenda_ur` escala para ingestão de arquivo sem medir primeiro.
    - **Decisão tomada no Plano 08:** opção (a) — `upsert_agenda_ur` usado como está, sem caminho de escrita em lote alternativo, mesma filosofia "não otimizar antes de medir" aplicada em outros pontos deste design. Reavaliar se o volume real de um arquivo AP005 de produção se mostrar lento.
14. **`consulta_agenda_ur` não é escrito para URs síncronas (achado na revisão final do Plano 06).** SPEC03 §9 define essa tabela como o vínculo N:N consulta↔UR com um `origem` cujo domínio inclui `SINCRONO`, mas `services/cerc/client.py` (Plano 06) só grava `agenda_ur`/`agenda_ur_pagamento` via `upsert_agenda_ur` e atualiza `consulta_agenda.qtd_urs_sincrono` — nunca insere em `consulta_agenda_ur`. Decisão consciente de escopo: o Plano 07 (webhook) é quem efetivamente popula essa tabela hoje (origem `WEBHOOK`), deixando-a parcialmente povoada até que um plano futuro decida se vale a pena espelhar o vínculo também no caminho síncrono (o `consulta_id` e todas as chaves da UR já estão disponíveis em `consultar_agenda` quando isso for revisitado) ou se `qtd_urs_sincrono` é suficiente como contagem por origem para o que o Plano 09 (`GET /api/v1/agendas/consultas/{id}`) precisa expor.
   **Decisão tomada no Plano 09:** o caminho síncrono (`services/cerc/client.py::_vincular_consulta_ur`) agora grava em `consulta_agenda_ur` com `origem='SINCRONO'` para cada UR persistida, espelhando o que o webhook (Plano 07) já faz com `origem='WEBHOOK'`. `GET /api/v1/agendas/consultas/{id}` usa essa tabela, agrupada por `origem`, como única fonte de verdade para a contagem por origem — `qtd_urs_sincrono` continua escrita por compatibilidade, mas não é mais lida por esse endpoint. Ver risco 19 para uma interação nova que essa escrita introduziu com o caminho do webhook.

15. **`consultar_agenda` podia deixar `consulta_agenda` presa em `PARCIAL` para sempre em falhas fora do catálogo CERC — corrigido.** `_criar_consulta_agenda` grava a linha em `PARCIAL` **antes** de chamar a CERC, justamente para garantir trilha de auditoria mesmo em falha (risco 5 do Plano 06). O `except CercConsultaError:` em `consultar_agenda` não cobria `CercTokenError` (de `services/cerc/token_provider.get_cerc_token`, uma exceção irmã, não subclasse) nem outras exceções que escapassem de `_chamar_cerc` (ex.: erro de banco dentro de `_registrar_requisicao`), deixando a linha permanentemente em `PARCIAL` nesses casos. **Corrigido** ampliando para `except Exception:` — qualquer falha ao obter o token ou chamar a CERC agora fecha a consulta como `ERRO` antes de propagar a exceção, com teste de regressão (`test_consultar_agenda_fecha_consulta_em_erro_quando_token_falha`).
16. **Inconsistência de auditoria em UR multi-titular parcialmente persistida (achado na mesma re-revisão).** No laço de `consultar_agenda`, se `upsert_agenda_ur` tiver sucesso para um titular de uma UR fracionada e falhar num titular seguinte da mesma UR, a linha do titular bem-sucedido fica persistida corretamente, mas a UR inteira ainda é gravada em `agenda_ur_rejeitada` como se tivesse sido totalmente rejeitada. Não é perda de dado (a linha persistida está correta) — é só o registro de auditoria que fica impreciso nesse caso específico. Baixa severidade, não bloqueante, registrado para não ser redescoberto do zero.
17. **Sem recuperação para publish do Pub/Sub que falhou silenciosamente (Plano 07, achado na revisão final).** `shared/pubsub_client.publish_webhook_agenda` é melhor-esforço por design — se falhar, a linha em `webhook_inbox` fica com `processado_em IS NULL` para sempre, sem nenhum job que a recupere (o índice `webhook_inbox (recebido_em) WHERE processado_em IS NULL`, de `02-agenda-schema-fixes.sql`, já antecipa esse varredor, que nunca foi construído). Pior: `varrer_completude` (também do Plano 07) fecha uma consulta como `COMPLETA` só pelo quiet period, sem checar se existem eventos de `webhook_inbox` ainda não processados para ela — ou seja, uma consulta pode ser marcada completa enquanto ainda há webhook perdido no caminho. Mitigação mínima para quando isso for revisitado: `varrer_completude` (ou um job novo) republicar/logar linhas de `webhook_inbox` com `processado_em IS NULL` mais velhas que alguns minutos, antes de fechar qualquer consulta.
18. **Lista fixa de tenants dos jobs periódicos vira um bug real assim que houver um segundo tenant (achado na revisão final do Plano 07).** `_TENANTS_JOBS_PERIODICOS` (`apps/agenda/views.py`) é uma lista fixa (mesmo ponto em aberto do §14 item 3), mas o receptor do webhook já roteia de verdade por `financiador_id` da URL. No dia em que um segundo tenant for onboardado sem entrar nessa lista, as consultas `ONLINE` dele nunca fecham (ficam `PARCIAL` para sempre), o que por sua vez faz `encontrar_consultas_candidatas` reexaminar um conjunto de candidatas cada vez maior a cada webhook recebido por QUALQUER tenant do mesmo banco (agravado pelo Fix 3 deste plano, que já filtra por `filtro_ufr` mas não pelo total de linhas `PARCIAL` acumuladas). Isso deixou de ser um "ponto em aberto" passivo e passou a ser um bug latente — resolver a lista de tenants (§14 item 3) antes ou junto do próximo plano que faça onboarding de um segundo tenant real.
19. **Escrita síncrona em `consulta_agenda_ur` (Plano 09) pode colidir com a escrita do webhook na mesma UR, mascarando uma atualização de frescor (achado na revisão final do Plano 09).** A chave primária de `consulta_agenda_ur` é `(consulta_id, entidade_registradora, cnpj_credenciadora, documento_ufr, documento_titular, codigo_arranjo, data_liquidacao)` — **`origem` não faz parte dela**. Antes do Plano 09, só o webhook escrevia nessa tabela, então a colisão nunca acontecia. Agora, uma consulta `ONLINE` que recebe uma UR no retorno síncrono grava um vínculo com `origem='SINCRONO'`; se um webhook chegar depois para essa mesma UR (a consulta ainda está `PARCIAL`, exatamente o estado que `encontrar_consultas_candidatas` casa), o `INSERT` do webhook em `consulta_agenda_ur` colide com a violação de chave única, `_violacao_unique` engole o erro e o código segue com `continue` — pulando também a atualização de `ultima_ur_em`/`qtd_urs_webhook` que viria logo depois (`apps/agenda/views.py`, `processar_webhook_agenda`). Consequência: o timer de quiet period do `varrer_completude` pode fechar essa consulta como `COMPLETA` usando um `ultima_ur_em` desatualizado, mesmo com atividade real de webhook acontecendo. Não é perda de dado (a UR em si já foi persistida corretamente pelos dois caminhos via `upsert_agenda_ur`/regra de frescor) — é uma imprecisão de timing na completude e na contagem por origem. Não corrigido no Plano 09 (exigiria mudar a lógica de correlação do webhook, Plano 07, com seu próprio ciclo de revisão) — mitigação mínima para quando isso for revisitado: no branch de violação única do webhook, atualizar `ultima_ur_em`/`qtd_urs_webhook` mesmo assim (e considerar promover o `origem` da linha existente para `WEBHOOK`) em vez de só `continue`.
20. **`GET /api/v1/agendas/consultas/{id}` faz uma consulta ao banco por UR vinculada para calcular o frescor (achado na revisão final do Plano 09).** `_contagem_e_frescor_por_origem` (`apps/agenda/views.py`) chama `_buscar_um` uma vez por linha de `consulta_agenda_ur` da consulta, em vez de uma busca em lote — decisão consciente (§2/§15 risco 2, "não otimizar antes de medir"; `shared/cloudsql_client.py` não tem suporte a `IN`/agregação, e o Plano 09 não estende esse arquivo). Para uma consulta `BATCH` de janela larga com curingas (`99T`) em credenciadoras/arranjos, isso pode ser centenas ou milhares de round trips síncronos numa única requisição HTTP. Reavaliar junto do Plano 10, que já vai precisar de agregação em lote para `GET /agendas/urs/posicao` — a mesma capacidade resolveria os dois.

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
9. **API interna — ciclo de vida da consulta** (`POST`/`GET /agendas/consultas`, `config/politicas-consulta`) — fecha risco 14.
10. **API interna — leitura/relatório** (`GET /agendas/urs`, `GET /agendas/urs/posicao`, `GET /compliance/relatorio`) + compliance.
11. **Observabilidade + testes de carga.**

Planos 3+ são escritos **depois** que o plano anterior estiver implementado e revisado — não faz sentido detalhar o plano 5 antes de saber que o 3 e o 4 realmente funcionaram (mesma prática já adotada nos dois serviços irmãos).
