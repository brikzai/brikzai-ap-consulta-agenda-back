# Design — Agenda Service (SPEC 03 / CERC-AP005)

> **Status:** aguardando revisão do usuário
> **Repositório:** `ap-back-consulta-agenda`
> **Specs de origem:** `docs/specs/SPEC-03-consulta-de-agenda-ap005.md` (funcional/CERC), `docs/specs/SPEC-04-modelo-de-dados.md` (DDL, autoritativa — substitui o §9 da SPEC 03)
> **Data:** 2026-08-24

## 1. Contexto e objetivo

Este serviço (`agenda-service`) implementa a **consulta de agenda de recebíveis** (CERC-AP005), um dos três serviços do produto:

| Serviço | Responsabilidade | Schema Postgres (SPEC 04) |
|---|---|---|
| `optin-service` | Opt-in/opt-out, auth centralizada (JWT), registro de financiadores e roteamento de tenant | `optin` (+ dono do `cerc` compartilhado) |
| `agenda-service` (este) | Consulta de agenda batch/online, webhook de agenda, ingestão AP005/AP005A/AP005B | `agenda` |
| `contrato-service` | Registro de contratos e garantias (SPEC 02) | `contrato` |

Este documento cobre **apenas** o `agenda-service`. As decisões sobre `optin-service` e `contrato-service` aparecem aqui só como **interfaces assumidas** (§14).

## 2. Escopo

**Dentro do escopo:**
- Consulta de agenda batch e online via `POST /v15/agenda/consultar` (cliente CERC)
- Recebimento e correlação do webhook `tipoEvento = agenda`
- Ingestão de arquivo AP005 / AP005A / AP005B
- API interna `/api/v1/agendas/*`
- Configuração self-service de política de modo (BATCH/ONLINE) por finalidade
- Compliance (trilha de auditoria, rate limit, relatório) e observabilidade do domínio de agenda

**Fora do escopo (dependências externas, ver §14):** emissão de JWT, registro de financiadores/tenant, opt-in/opt-out, registro de contratos.

## 3. Decisões transversais já fechadas

1. **Stack:** Python + Django.
2. **Autenticação entre serviços:** `optin-service` centraliza as credenciais OAuth2 da CERC (`TokenProvider`) e emite um JWT interno; os demais serviços **validam localmente** (JWKS), sem round-trip síncrono por requisição, exceto para obter token CERC (§8 abaixo).
3. **Multi-tenancy híbrida:** financiadores grandes/regulados recebem **schema Postgres dedicado**; financiadores menores compartilham o schema `agenda` com coluna `cnpj_financiador` + **Row-Level Security**. A decisão de qual tenant usa qual modalidade, e o registro de financiadores, é responsabilidade do `optin-service` — o `agenda-service` só **consome** essa decisão via claim do JWT (`tenant_schema`) e session var Postgres (`app.cnpj_financiador`) quando aplicável.
4. **Modelo de dados:** SPEC 04 é autoritativa. O DDL do schema `agenda` roda via migrations `RunSQL` versionadas; os modelos Django mapeiam essas tabelas com `managed=False`.

## 4. Arquitetura de componentes (apps Django)

| App | Responsabilidade |
|---|---|
| `consultas` | Orquestra consulta batch/online; roda validações locais `A01`–`A10`; grava `consulta_agenda` |
| `webhooks` | Recebe `tipoEvento=agenda`; grava em `cerc.webhook_inbox` (idempotente); dispara processamento assíncrono |
| `correlacao` | Casa UR de webhook com consultas em janela; grava `consulta_agenda_ur` / `agenda_ur_orfa` |
| `ingestao_arquivo` | Pipeline AP005/AP005A/AP005B — streaming, gzip, idempotência |
| `agenda_ur` | Modelo de domínio + repositório de upsert (regra de frescor/precedência) |
| `politica_consulta` | Configuração self-service de modo permitido por finalidade |
| `api_interna` | Endpoints `/api/v1/agendas/*` e `/api/v1/config/*` |
| `compliance` | Rate limit por UFR, trilha de auditoria, relatório exportável |
| `observabilidade` | Métricas Prometheus, logging estruturado |

## 5. Camada de dados

- DDL do schema `agenda` (SPEC 04 §5.4) aplicado via migrations `RunSQL`, versionado no repositório. Django não gerencia particionamento, `PARTITION BY`, `ON CONFLICT ... WHERE` ou funções — isso está fora do que o schema editor do Django expressa.
- Modelos Django para leitura/query padrão: `managed=False`, mapeando 1:1 as tabelas de `agenda.*`.
- Escritas críticas (upsert de `agenda_ur`/`agenda_ur_pagamento`, carga em massa de arquivo) passam por um **repositório com SQL explícito** — não por `.save()` — porque a precedência de frescor (`WEBHOOK > SINCRONO > ARQUIVO`, SPEC 04 §5.5) não é expressável via ORM padrão.
- Views de fronteira consumidas (somente leitura): `optin.v_base_autorizativa`. View de fronteira exposta (somente leitura para `contrato-service`): `agenda.v_posicao_ufr`.
- `identificador_cerc_contrato` referencia `contrato.contrato.id_contrato_cerc` **por valor**, sem FK (dados chegam por canais independentes, fora de ordem).

## 6. Roteamento de tenant

Um middleware, no início de cada requisição:
1. Lê a claim `tenant_schema` do JWT validado.
2. Executa `SET search_path TO <tenant_schema>, cerc, public` na conexão — `cerc` sempre presente, pois é infraestrutura compartilhada e não varia por tenant.
3. Se o tenant for do tipo compartilhado (schema `agenda` pool), também executa `SET app.cnpj_financiador = '<cnpj>'` para as políticas de RLS.
4. Ao final da requisição, o `search_path` é resetado (uma conexão nunca deve "herdar" o schema de uma requisição anterior).

Consequência de design: como o isolamento de tenant acontece na fronteira (schema dedicado ou RLS), as leituras de `GET /urs` e `GET /urs/posicao` **não** precisam reconferir autorização a cada linha — o dado já só existe naquele tenant porque passou pela checagem de base autorizativa (`A07`) no momento da escrita (consulta ou arquivo).

## 7. Política de consulta (self-service, fail-closed)

Nova tabela `agenda.politica_consulta`:

```sql
CREATE TABLE agenda.politica_consulta (
  id                 cerc.ulid PRIMARY KEY,
  cnpj_financiador   cerc.documento NOT NULL,  -- constante/implícito em schema dedicado
  motivo             TEXT NOT NULL,
  modos_permitidos   TEXT[] NOT NULL CHECK (modos_permitidos <@ ARRAY['BATCH','ONLINE']),
  ativo              BOOLEAN NOT NULL DEFAULT true,
  criado_em          TIMESTAMPTZ NOT NULL DEFAULT now(),
  atualizado_em      TIMESTAMPTZ NOT NULL DEFAULT now(),
  versao             INT NOT NULL DEFAULT 1,
  CONSTRAINT politica_unica UNIQUE (cnpj_financiador, motivo)
);
```

Regras:
- **Fail-closed:** sem política ativa para `(financiador, motivo)` → `403 POLITICA_NAO_CONFIGURADA`, sem chamar a CERC. Nova regra local `A10`, ao lado de `A01`–`A09` (SPEC 03 §10.1).
- O campo `modo` continua vindo do chamador (compatibilidade com SPEC 03 §7.1), mas é **validado** contra `modos_permitidos` da política daquele `motivo` — o chamador não escolhe livremente, escolhe dentro do que a política autoriza.
- Self-service: cada financiador só vê/edita a própria política, escopado pelo JWT.
- Endpoints: `GET/PUT/DELETE /api/v1/config/politicas-consulta`.

## 8. Integração com a CERC

**Infra assíncrona:** Celery + Redis para todo processamento fora do caminho síncrono da requisição (worker de webhook, varredura de completude, reprocessamento com backoff).

**Cliente de consulta (`POST /v15/agenda/consultar`):**
1. Roda `A01`–`A10` localmente antes de qualquer chamada externa.
2. Obtém token CERC do financiador chamando um endpoint interno do `optin-service` (interface assumida, §14) — `agenda-service` não guarda credencial CERC própria.
3. Registra toda chamada (request/response/tentativa/duração) em `cerc.cerc_requisicao`.
4. **Batch** (`online=false`/omitido): chama, persiste com `origem='SINCRONO'` (upsert §5.5 SPEC 04), devolve `200` consolidado.
5. **Online** (`online=true`): chama, persiste o retorno síncrono (`origem='SINCRONO'`), cria `consulta_agenda` em `PARCIAL`, devolve `202` com `consultaId` imediatamente — nunca espera o webhook.
6. Mapeamento de erros CERC (SPEC 03 §10): `105001` → sucesso vazio (`200`, lista vazia); `105003`/`105999`/`105998` → retentável com backoff; `105802` → não deveria ocorrer (bloqueado antes por `A07`), e se ocorrer é alerta crítico; demais códigos de validação → `422` local.

**Receptor de webhook (`tipoEvento=agenda`):**
- Handler HTTP grava o payload bruto em `cerc.webhook_inbox` (com `hash_dedupe`) e retorna `2xx` em <200ms. Nenhuma lógica de negócio no caminho síncrono.
- Worker Celery consome a fila (índice parcial `WHERE processado_em IS NULL`) e, por UR:
  - Correlação (SPEC 03 §5.4): casa `(instituicaoCredenciadora, codigoArranjoPagamento, documentoUsuarioFinalRecebedor, documentoTitular, dataLiquidacao)` contra `consulta_agenda` em `PARCIAL`/`ONLINE`, tratando `99T` como universo e a janela de datas como intervalo.
  - 0 casamentos → `agenda_ur_orfa` (nunca descarta).
  - ≥1 casamento → grava em `consulta_agenda_ur` para cada consulta casada; upsert em `agenda_ur`/`agenda_ur_pagamento` com `origem='WEBHOOK'`.
  - Deduplicação por `hash_dedupe` reforçada pela `UNIQUE` do banco.

**Critério de completude (sem sinal de fim, SPEC 03 §5.5):** Celery Beat varre `consulta_agenda WHERE status='PARCIAL'` a cada ~15–30s (usa o índice já definido na SPEC 04):
- `now() - ultima_ur_em > 90s` → `COMPLETA`
- `now() - iniciada_em > 15min` → `COMPLETA_COM_TIMEOUT` + alerta

**Rate limit (`A08`, 10 consultas online/UFR/dia):** contagem em `consulta_agenda` via índice parcial `(filtro_ufr, iniciada_em) WHERE modo='ONLINE'` — sem contador externo nesse volume inicial. "Dia" é o dia calendário em `America/Sao_Paulo` (fuso de negócio da CERC), não UTC — evita que a virada de dia em UTC libere consultas extras às 21h de Brasília.

## 9. Ingestão de arquivo (AP005 / AP005A / AP005B)

- **Chegada:** Celery Beat faz polling em SFTP/Bucket/Connect:Direct por `CERC-AP005[A|B]_{ident_ic}_{data_req}_{seq}_ret.csv[.gz]` em `/informacoes_agendas/saida/`. *(Conexão/credenciais reais são dependência de infra a confirmar — não bloqueia este design.)* `ident_ic` resolve o tenant (financiador → schema).
- **Idempotência:** checa `agenda.arquivo_agenda_processado` por `(tipo_leiaute, ident_ic, data_req, seq)` antes de processar.
- **Streaming:** AP005A/AP005B vêm em `.gz`, descompactados via streaming — nunca materializa o arquivo inteiro em memória.
- **Detecção de layout** pela contagem de colunas: completo (16 col.) vs. reduzido "sem agenda" (3 col., resultado válido) vs. com/sem coluna 12.16.
- **Origem** (`AP005`/`AP005A`/`AP005B`) vem do nome do arquivo — um único parser para os três.
- `tipoInformacaoPagamento` aceito `1`–`8` em arquivo e webhook; fora disso → `agenda_ur_rejeitada`, nunca exceção não tratada.
- Linha inválida → `agenda_ur_rejeitada` com motivo; arquivo continua. Alerta se rejeição > 0,5%.
- **Carga em massa:** `COPY` para tabela temporária *unlogged*, `DISTINCT ON` pela chave natural (ordenando por `data_hora_ultima_atualizacao DESC`), depois um único `INSERT ... ON CONFLICT` com a cláusula de precedência de frescor. Nunca linha a linha.
- **Prioridade de pipeline:** AP005 (opt-in) antes de AP005A (contrato) antes de AP005B (fumaça), via filas Celery com prioridades distintas.

## 10. API interna (`/api/v1/agendas/*`, `/api/v1/config/*`, `/api/v1/compliance/*`)

| Endpoint | Descrição |
|---|---|
| `POST /api/v1/agendas/consultas` | Dispara consulta batch/online (valida `A01`–`A10`, chama CERC) |
| `GET /api/v1/agendas/consultas/{id}` | Status consolidado (`PARCIAL`/`COMPLETA`/`COMPLETA_COM_TIMEOUT`), contagem por origem |
| `GET /api/v1/agendas/urs` | Repositório consolidado, filtros + paginação por cursor (`limit ≤ 1000`) |
| `GET /api/v1/agendas/urs/posicao` | Visão agregada de crédito por UFR/janela, fumaça sempre segregada |
| `GET/PUT/DELETE /api/v1/config/politicas-consulta` | Política self-service de modo por finalidade |
| `GET /api/v1/compliance/relatorio` | Relatório de consultas por período/UFR/ator (síncrono/paginado inicialmente) |

## 11. Compliance e auditoria

- `ator` e `origem_ip` preenchidos a partir do JWT/request no momento da consulta, gravados em `consulta_agenda` — retenção de 5 anos, sem expurgo (SPEC 04 §7.1).
- Relatório exportável evolui para job assíncrono com link de download se o volume exigir — não construído de antemão sem necessidade comprovada.

## 12. Observabilidade

- Métricas via `django-prometheus`: `agenda_consultas_total{modo,resultado}`, `agenda_cerc_latency_seconds{modo}` (SLO p95: batch < 3s, online < 8s), `agenda_webhook_urs_total`, `agenda_webhook_orfas_total`, `agenda_arquivo_linhas_total{leiaute,resultado}`, `agenda_ur_frescor_horas`.
- Alertas (Alertmanager) espelhando a tabela de severidade da SPEC 03 §11 — `105801` e "consulta online sem base autorizativa" são os críticos de compliance.
- Logs estruturados (JSON) com `correlacao_id`; documentos sempre mascarados no log.

## 13. Testes

- **Unitários:** regras `A01`–`A10`; parser AP005 (todas as variações de layout); branching de `tipoInformacaoPagamento`; upsert de frescor/precedência; algoritmo de correlação (`99T`, casamento múltiplo).
- **Integração:** cenários IT-01 a IT-18 da SPEC 03 §12.2, com Postgres real (testcontainers — valida partição/upsert/RLS de fato) e stub da API CERC (respostas gravadas, incluindo os CNPJs de homologação do §5.6 para exercitar o webhook).
- **Carga:** receptor de webhook a 500 req/s (k6/locust); ingestor com 5M linhas validando memória constante.

## 14. Interfaces assumidas de outros serviços (dependências externas)

Estas são **suposições de contrato**, não implementadas por este serviço — validar com os times de `optin-service` antes da integração real:

1. Endpoint interno para obter token CERC válido de um financiador (reuso do `TokenProvider`).
2. Registro de financiadores + decisão de tenant (`tenant_schema`) exposta como claim do JWT.
3. View `optin.v_base_autorizativa` populada e mantida pelo `optin-service`.
4. Conexão real (credenciais SFTP/Bucket/Connect:Direct) para chegada do arquivo AP005 — a definir.

## 15. Riscos conhecidos (herdados da SPEC 04 + específicos deste design)

1. `agenda_ur` sem `id` técnico — PK composta de 6 colunas, aceito conscientemente (SPEC 04 §11.1).
2. Correlação consulta↔webhook é heurística (SPEC 03 §5.4) — se a CERC expuser um id de consulta no futuro, simplifica.
3. Volume de `agenda_ur_pagamento` pode chegar a 5–10× a estimativa em cenário de ônus empilhados — instrumentar desde o primeiro mês.
4. RLS em schema compartilhado depende de toda query passar pela sessão com `app.cnpj_financiador` setado corretamente pelo middleware — testar explicitamente o caminho de falha (sessão sem a variável setada deve **negar** acesso, não liberar).

## 16. Ordem de implementação recomendada

1. Camada de dados (schema `agenda`, migrations `RunSQL`, modelos `managed=False`, repositório de upsert)
2. Roteamento de tenant (middleware + RLS de teste)
3. Cliente de consulta CERC (batch primeiro, depois online) + validações `A01`–`A10`
4. Webhook + correlação + completude
5. Ingestão de arquivo AP005/AP005A/AP005B
6. API interna completa + política de consulta self-service
7. Compliance, observabilidade, testes de carga
