# SPEC 04 — Modelo de Dados Consolidado (Integração CERC / Arranjos de Pagamento)

> **Status:** pronta para implementação
> **Escopo:** persistência das SPECs 01 (Opt-in), 02 (Contratos/AP007) e 03 (Agenda/AP005)
> **SGBD alvo:** PostgreSQL 15+ (o modelo usa `JSONB`, arrays, particionamento declarativo e `MERGE`/`ON CONFLICT`)
> **Topologia:** um cluster, três schemas de domínio + um compartilhado
> **Dimensionamento:** projetado para **acima de 500M URs**, particionado desde o dia zero

Esta spec **substitui** as seções de DDL das anteriores: SPEC 01 §6, SPEC 02 §11 e SPEC 03 §9. Onde houver divergência, **este documento prevalece** — inclusive na correção descrita em §0.

---

## 0. Correções em relação às specs anteriores

| Item | Onde estava | Correção |
|---|---|---|
| `agenda_ur_pagamento` com `PRIMARY KEY (..., COALESCE(indicador_efeitos_contrato,''))` | SPEC 03 §9 | **Inválido em PostgreSQL** — PK não aceita expressão. Corrigido para coluna `NOT NULL DEFAULT ''` (§5.3) |
| Tabelas sem particionamento | SPEC 01/02/03 | Todas as tabelas de alto volume passam a ser particionadas (§4) |
| `CHECK (vigencia_inicio >= data_assinatura)` em `optin` | SPEC 01 §6 | Mantido, mas rebaixado a validação de aplicação **também** — o `CHECK` não cobre atualização parcial vinda de reconciliação |
| Tipos `TEXT` para códigos de domínio | todas | Mantido `TEXT` + `CHECK`, com justificativa em §3.4 (não usar `ENUM` nativo) |

---

## 1. Princípios inegociáveis

1. **Dinheiro é `NUMERIC(18,2)`.** Nunca `float`, `double`, `real` ou `money`. Verificação estática no CI: qualquer coluna com nome contendo `valor|saldo|limite|montante` e tipo não-`NUMERIC` reprova o build.
2. **Tempo é `TIMESTAMPTZ`, banco em UTC.** Datas de negócio da CERC (`AAAA-MM-DD`) são `DATE` puro — não converter para timestamp, senão o fuso de São Paulo desloca a data de liquidação em um dia.
3. **Ids técnicos são ULID em `TEXT`** (26 chars, ordenável por tempo, gerado na aplicação). Não usar `SERIAL` para agregados — atrapalha idempotência e replicação. `BIGSERIAL` só em tabelas de log append-only.
4. **Documentos (CPF/CNPJ) são `TEXT` normalizado**: só dígitos, zero-padded, 8 (raiz), 11 (CPF) ou 14 (CNPJ). Nunca gravar com máscara. `CHECK (documento ~ '^[0-9]{8}$|^[0-9]{11}$|^[0-9]{14}$')`.
5. **A chave natural da CERC é a chave do banco.** Onde a CERC define unicidade (`referenciaExterna`, `protocolo`, a chave da UR), o banco reflete isso em constraint — não em código de aplicação.
6. **Toda tabela de alto volume nasce particionada.** Converter tabela grande depois exige janela de manutenção que você não vai ter.

---

## 2. Topologia

Um cluster PostgreSQL, quatro schemas:

| Schema | Conteúdo | Dono (serviço) |
|---|---|---|
| `cerc` | infraestrutura compartilhada: token, requisições, webhook inbox, domínios | plataforma |
| `optin` | SPEC 01 — opt-in, opt-out | `optin-service` |
| `contrato` | SPEC 02 — contratos, garantias, URs alcançadas | `contrato-service` |
| `agenda` | SPEC 03 — consultas, URs, pagamentos, arquivos | `agenda-service` |

**Por que um cluster e não três bancos:** a junção que dá valor ao produto é `agenda.agenda_ur_pagamento.identificador_cerc_contrato` × `contrato.contrato.id_contrato_cerc` (coluna 12.16 do AP005). Em bancos separados, isso vira replicação ou chamada de API por linha — inviável no volume alvo.

**Isolamento sem separação física:** cada serviço tem um role com `USAGE` apenas no próprio schema + `SELECT` no `cerc` e nas *views* de leitura cruzada (§8). Nenhum serviço escreve no schema de outro. Isso preserva a fronteira lógica e mantém a opção de separar depois.

```sql
CREATE SCHEMA cerc;
CREATE SCHEMA optin;
CREATE SCHEMA contrato;
CREATE SCHEMA agenda;

-- ordem de criação: cerc → optin → contrato → agenda
-- (agenda referencia contrato apenas por valor, não por FK — ver §8.2)
```

---

## 3. Convenções

### 3.1 Domínios reutilizáveis

```sql
CREATE DOMAIN cerc.documento AS TEXT
  CHECK (VALUE ~ '^[0-9]{8}$|^[0-9]{11}$|^[0-9]{14}$');

CREATE DOMAIN cerc.valor_monetario AS NUMERIC(18,2)
  CHECK (VALUE >= 0);

CREATE DOMAIN cerc.ulid AS TEXT
  CHECK (char_length(VALUE) = 26);

CREATE DOMAIN cerc.protocolo AS TEXT          -- GUID gerado pela CERC
  CHECK (VALUE ~ '^[0-9a-fA-F-]{36}$');
```

> `valor_monetario` com `CHECK (VALUE >= 0)` cobre a regra da CERC de recusar valores negativos. Onde a CERC exige ≥ 0.01, o `CHECK` específico vai na coluna.

### 3.2 Nomenclatura

`snake_case`; tabelas no singular; PK sempre `id` quando técnica; colunas de data com sufixo `_em` (timestamp) ou `_data`/prefixo `data_` (date); booleanos com prefixo verbal (`repactuacao` é exceção herdada do domínio CERC).

### 3.3 Auditoria mínima em todo agregado

`criado_em TIMESTAMPTZ NOT NULL DEFAULT now()`, `atualizado_em TIMESTAMPTZ NOT NULL DEFAULT now()` mantido por trigger, `versao INT NOT NULL DEFAULT 1` para lock otimista.

```sql
CREATE FUNCTION cerc.touch() RETURNS TRIGGER AS $$
BEGIN
  NEW.atualizado_em := now();
  NEW.versao := OLD.versao + 1;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
```

### 3.4 Por que `TEXT` + `CHECK` e não `ENUM` nativo

Os domínios da CERC mudam por comunicado (novos arranjos, o `tipoEfeito = 8` ainda em aberto). `ALTER TYPE ... ADD VALUE` não roda dentro de transação e não permite remoção. `TEXT` + `CHECK` permite `ALTER TABLE ... DROP CONSTRAINT / ADD CONSTRAINT NOT VALID` sem lock longo. Para o domínio **volátil** (arranjos de pagamento), nem `CHECK`: tabela `cerc.dominio_arranjo` sincronizável.

---

## 4. Estratégia de particionamento

### 4.1 O que particionar e por quê

| Tabela | Chave de partição | Granularidade | Motivo |
|---|---|---|---|
| `agenda.agenda_ur` | `data_liquidacao` | **RANGE mensal** | Dominante em volume; 90 % das queries filtram por janela de liquidação; expurgo por `DROP PARTITION` |
| `agenda.agenda_ur_pagamento` | `data_liquidacao` | RANGE mensal | Alinhada à pai; permite *partition-wise join* |
| `agenda.consulta_agenda_ur` | `recebida_em` | RANGE mensal | Alto churn, vida curta |
| `cerc.webhook_inbox` | `recebido_em` | **RANGE semanal** | Rajadas de webhook; expurgo agressivo (90 dias) |
| `cerc.cerc_requisicao` | `criado_em` | RANGE mensal | Retenção regulatória de 5 anos = 60 partições |
| `contrato.garantia_ur` | `snapshot_em` | RANGE mensal | Snapshots acumulam rápido |
| `contrato.contrato_evento` | `ocorrido_em` | RANGE mensal | Log append-only |

**Não particionar:** `optin.*`, `contrato.contrato`, `contrato.garantia`, `agenda.consulta_agenda`. Volume baixo (milhões, não bilhões) e são alvo de FK — particionar complica sem ganho.

### 4.2 Por que `data_liquidacao` e não `atualizado_em` em `agenda_ur`

A UR é atualizada muitas vezes ao longo da vida (constituição, oneração, liquidação), mas sua `data_liquidacao` **nunca muda** — é parte da identidade. Particionar por uma coluna mutável causaria movimentação de linha entre partições a cada update, que no Postgres é `DELETE + INSERT` com reescrita de todos os índices. Com `data_liquidacao`, o update é sempre local à partição.

### 4.3 Janela e ciclo de vida das partições

```
[ passado profundo ]  [ 13 meses quentes ]  [ futuro: +24 meses ]
       ARQUIVO              OPERACIONAL           PRÉ-CRIADO
```

- **Futuro:** pré-criar 24 meses à frente. Agendas de arranjo parcelado projetam liquidações longas; `INSERT` sem partição destino falha (`no partition of relation found`) — o pior erro possível num ingestor batch às 4h da manhã.
- **Quente:** 13 meses retroativos em disco rápido.
- **Frio:** partições mais antigas movidas para tablespace lento ou exportadas em Parquet e removidas (§7.3).

```sql
-- Criação automatizada (job mensal). Usar pg_partman ou o script abaixo.
CREATE TABLE agenda.agenda_ur_2026_09 PARTITION OF agenda.agenda_ur
  FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');

-- DEFAULT partition como rede de segurança — monitorar: linha caindo
-- aqui indica falha do job de pré-criação.
CREATE TABLE agenda.agenda_ur_default PARTITION OF agenda.agenda_ur DEFAULT;
```

> **Alerta obrigatório:** `SELECT count(*) FROM agenda.agenda_ur_default` > 0 → severidade alta. A partição default existe para não derrubar a ingestão, não para receber dados em regime.

**Recomendação:** usar **`pg_partman`** em vez de script próprio. Cria, ativa retenção e move para tablespace frio de forma declarativa.

### 4.4 Constraint obrigatória

Em tabela particionada, **a chave de partição precisa estar em toda PK e em todo índice único**. Por isso `data_liquidacao` compõe a PK de `agenda_ur` e `agenda_ur_pagamento` — não é redundância de modelagem, é requisito do Postgres.

---

## 5. DDL

### 5.1 Schema `cerc` — compartilhado

```sql
-- Domínio de arranjos: sincronizável, nunca hardcode
CREATE TABLE cerc.dominio_arranjo (
  codigo        TEXT PRIMARY KEY,
  descricao     TEXT,
  ativo         BOOLEAN NOT NULL DEFAULT true,
  sincronizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Semear com o domínio v1.5 + o curinga 99T.

-- Participantes do SLC (valida domicílio de pagamento — SPEC 02 §4.3)
CREATE TABLE cerc.participante_slc (
  ispb          TEXT PRIMARY KEY CHECK (ispb ~ '^[0-9]{8}$'),
  compe         TEXT CHECK (compe ~ '^[0-9]{3}$'),
  nome          TEXT NOT NULL,
  ativo         BOOLEAN NOT NULL DEFAULT true,
  sincronizado_em TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trilha de toda chamada à CERC (auditoria + replay). Retenção 5 anos.
CREATE TABLE cerc.cerc_requisicao (
  id            cerc.ulid    NOT NULL,
  recurso       TEXT         NOT NULL,   -- opt_in|opt_out|contratos|agenda_consultar|contrato_consultar
  operacao      TEXT,                    -- C|A|I|B|S|P|R quando aplicável
  correlacao_id TEXT         NOT NULL,
  ambiente      TEXT         NOT NULL,   -- HOMOLOGACAO|PRODUCAO
  http_status   INT,
  tentativa     INT          NOT NULL DEFAULT 1,
  duracao_ms    INT,
  request_body  JSONB        NOT NULL,
  response_body JSONB,
  criado_em     TIMESTAMPTZ  NOT NULL DEFAULT now(),
  PRIMARY KEY (id, criado_em)
) PARTITION BY RANGE (criado_em);

CREATE INDEX ON cerc.cerc_requisicao (correlacao_id);
CREATE INDEX ON cerc.cerc_requisicao (recurso, criado_em DESC);

-- Inbox de webhooks: grava ANTES de processar (SPEC 01 §4.4)
CREATE TABLE cerc.webhook_inbox (
  id               cerc.ulid   NOT NULL,
  tipo_evento      TEXT        NOT NULL,  -- contrato|agenda|notificacao|efeitoContrato|testeCerc
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

-- Índice parcial: a fila de trabalho é minúscula perto do total
CREATE INDEX ON cerc.webhook_inbox (recebido_em)
  WHERE processado_em IS NULL;
```

> **`webhook_inbox` é a tabela mais crítica do sistema.** A CERC tenta 5 vezes e desiste; um `INSERT` que falha é um evento perdido para sempre. Consequências no modelo: sem FK, sem trigger, sem validação — só `INSERT` e retorno. Toda a lógica fica no consumidor.

### 5.2 Schema `optin` (SPEC 01)

```sql
CREATE TABLE optin.optin (
  id                 cerc.ulid PRIMARY KEY,
  referencia_externa TEXT UNIQUE NOT NULL,
  protocolo_cerc     cerc.protocolo UNIQUE,
  origem             TEXT NOT NULL CHECK (origem IN ('OPTIN','CONTRATO')),
  status             TEXT NOT NULL CHECK (status IN
    ('PENDENTE','ATIVO','EXPIRADO','ENCERRADO','REJEITADO','FALHA_ENVIO')),
  cnpj_solicitante   cerc.documento NOT NULL,
  cnpj_financiador   cerc.documento NOT NULL,
  documento_ufr      cerc.documento NOT NULL,
  documento_ufr_tipo TEXT NOT NULL CHECK (documento_ufr_tipo IN ('CPF','CNPJ','CNPJ_RAIZ')),
  documento_titular  cerc.documento,
  data_assinatura    DATE NOT NULL,
  vigencia_inicio    DATE NOT NULL,
  vigencia_fim       DATE NOT NULL,
  carteira           TEXT,
  evidencia_id       TEXT NOT NULL,
  contrato_id        cerc.ulid,              -- preenchido quando origem = CONTRATO
  criado_em          TIMESTAMPTZ NOT NULL DEFAULT now(),
  atualizado_em      TIMESTAMPTZ NOT NULL DEFAULT now(),
  versao             INT NOT NULL DEFAULT 1,
  CONSTRAINT vigencia_coerente CHECK (vigencia_fim >= vigencia_inicio),
  CONSTRAINT inicio_apos_assinatura CHECK (vigencia_inicio >= data_assinatura),
  CONSTRAINT contrato_so_se_origem_contrato
    CHECK ((origem = 'CONTRATO') = (contrato_id IS NOT NULL))
);

CREATE TABLE optin.optin_credenciadora (
  optin_id cerc.ulid NOT NULL REFERENCES optin.optin(id) ON DELETE CASCADE,
  cnpj     TEXT NOT NULL,   -- documento OU '99T'
  PRIMARY KEY (optin_id, cnpj)
);

CREATE TABLE optin.optin_arranjo (
  optin_id cerc.ulid NOT NULL REFERENCES optin.optin(id) ON DELETE CASCADE,
  codigo   TEXT NOT NULL REFERENCES cerc.dominio_arranjo(codigo),
  PRIMARY KEY (optin_id, codigo)
);

CREATE TABLE optin.optout (
  id                 cerc.ulid PRIMARY KEY,
  optin_id           cerc.ulid NOT NULL REFERENCES optin.optin(id),
  referencia_externa TEXT UNIQUE NOT NULL,
  protocolo_cerc     cerc.protocolo,
  status             TEXT NOT NULL CHECK (status IN ('PENDENTE','CONFIRMADO','REJEITADO')),
  criado_em          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Índices por padrão de acesso
CREATE INDEX ON optin.optin (documento_ufr, status);
CREATE INDEX ON optin.optin (status) WHERE status IN ('PENDENTE','FALHA_ENVIO');
CREATE INDEX ON optin.optin USING gist (daterange(vigencia_inicio, vigencia_fim, '[]'))
  WHERE status = 'ATIVO';
```

> O índice **GiST sobre `daterange`** é o que torna viável a verificação anti-duplicidade (SPEC 01 §5.6) e a validação de base autorizativa (SPEC 03 §7.1) — as duas fazem "existe opt-in ativo cuja vigência intersecta esta janela?". Sem ele, vira *seq scan* a cada consulta de agenda.

### 5.3 Schema `contrato` (SPEC 02)

```sql
CREATE TABLE contrato.contrato (
  id                        cerc.ulid PRIMARY KEY,
  referencia_externa        TEXT UNIQUE NOT NULL,
  identificador_contrato    TEXT NOT NULL,
  protocolo_cerc            cerc.protocolo,
  id_contrato_cerc          TEXT UNIQUE,       -- junção com agenda (col. 12.16)
  status                    TEXT NOT NULL CHECK (status IN
    ('ENVIANDO','AGUARDANDO_WEBHOOK','REGISTRADO','REJEITADO','REJEITADO_ESTRUTURAL',
     'PENDENTE_CONCILIACAO','ATUALIZANDO','INATIVADO','BAIXADO',
     'RESILIDO_PARCIAL','RESILIDO_TOTAL')),
  status_garantia           TEXT CHECK (status_garantia IN
    ('NAO_APLICAVEL','SUFICIENTE','INSUFICIENTE','EXCESSO')),
  cnpj_participante         cerc.documento NOT NULL,
  documento_contratante     cerc.documento NOT NULL,
  cnpj_detentor             cerc.documento NOT NULL,
  tipo_efeito               TEXT NOT NULL CHECK (tipo_efeito IN ('1','2','3','4','8')),
  modalidade_operacao       TEXT NOT NULL CHECK (modalidade_operacao IN ('1','2','3')),
  gestao_entidade_registradora TEXT NOT NULL CHECK (gestao_entidade_registradora IN ('1','2','3')),
  tipo_servico              TEXT CHECK (tipo_servico IN ('1','2','3')),
  saldo_devedor             cerc.valor_monetario NOT NULL CHECK (saldo_devedor >= 0.01),
  limite_operacao_garantida cerc.valor_monetario NOT NULL CHECK (limite_operacao_garantida >= 0.01),
  valor_mantido             cerc.valor_monetario NOT NULL CHECK (valor_mantido >= 0.01),
  data_assinatura           DATE NOT NULL,
  data_vencimento           DATE NOT NULL,
  repactuacao               BOOLEAN NOT NULL,
  carteira                  TEXT,
  tipo_avaliacao            TEXT,
  taxa_juros                NUMERIC(8,2),
  indexador                 TEXT CHECK (indexador IN ('1','2','3','4','5','6','7','8')),
  qtd_urs_alcancadas        INT,
  valor_urs_alcancadas      cerc.valor_monetario,
  resultado_distribuicao    TEXT CHECK (resultado_distribuicao IN ('0','1','2','3')),
  ind_sobrecolateral        NUMERIC(12,4),
  enviado_em                TIMESTAMPTZ,
  confirmado_em             TIMESTAMPTZ,
  criado_em                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  atualizado_em             TIMESTAMPTZ NOT NULL DEFAULT now(),
  versao                    INT NOT NULL DEFAULT 1,
  CONSTRAINT chave_cerc UNIQUE (cnpj_participante, identificador_contrato),
  CONSTRAINT vencimento_apos_assinatura CHECK (data_vencimento >= data_assinatura)
);
```

> `tipo_efeito` inclui `'8'` (promessa de cessão) porque o **AP013 e o AP005 entregam esse valor**. A rejeição de `8` no *envio* é regra de aplicação (SPEC 02 §12.1), não de banco — um `CHECK` sem `8` faria a ingestão de arquivo quebrar.

```sql
CREATE TABLE contrato.contrato_contrato_anterior (
  contrato_id            cerc.ulid NOT NULL REFERENCES contrato.contrato(id) ON DELETE CASCADE,
  identificador_anterior TEXT NOT NULL,
  PRIMARY KEY (contrato_id, identificador_anterior)
);

CREATE TABLE contrato.contrato_parcela (
  contrato_id cerc.ulid NOT NULL REFERENCES contrato.contrato(id) ON DELETE CASCADE,
  vencimento  DATE NOT NULL,
  valor       cerc.valor_monetario NOT NULL CHECK (valor >= 0.01),
  PRIMARY KEY (contrato_id, vencimento)
);

CREATE TABLE contrato.contrato_domicilio (
  contrato_id              cerc.ulid PRIMARY KEY REFERENCES contrato.contrato(id) ON DELETE CASCADE,
  numero_documento_titular cerc.documento NOT NULL,
  nome_titular             TEXT,
  tipo_conta               TEXT NOT NULL CHECK (tipo_conta IN ('CC','CD','PG','PP')),
  compe                    TEXT CHECK (compe ~ '^[0-9]{3}$'),
  ispb                     TEXT NOT NULL REFERENCES cerc.participante_slc(ispb),
  agencia                  TEXT CHECK (char_length(agencia) <= 8),
  numero_conta             TEXT NOT NULL
);

CREATE TABLE contrato.garantia (
  id                       cerc.ulid PRIMARY KEY,
  contrato_id              cerc.ulid NOT NULL REFERENCES contrato.contrato(id) ON DELETE CASCADE,
  referencia_externa       TEXT NOT NULL,
  regras_divisao           TEXT NOT NULL CHECK (regras_divisao IN ('1','2')),
  valor_a_onerar           NUMERIC(18,2) NOT NULL CHECK (valor_a_onerar >= 0),
  tipo_distribuicao        TEXT CHECK (tipo_distribuicao IN
                             ('padrao_empilhamento_ap','padrao_pro_rata_ap')),
  def_lista_credenciadoras TEXT[] NOT NULL CHECK (array_length(def_lista_credenciadoras,1) >= 1),
  def_lista_arranjos       TEXT[] NOT NULL CHECK (array_length(def_lista_arranjos,1) >= 1),
  def_documento_ufr        cerc.documento,
  def_documento_titular    cerc.documento,
  def_data_inicio          DATE NOT NULL,
  def_data_fim             DATE NOT NULL,
  CONSTRAINT ref_unica_no_contrato UNIQUE (contrato_id, referencia_externa),
  CONSTRAINT def_datas_coerentes CHECK (def_data_fim >= def_data_inicio),
  CONSTRAINT percentual_ate_100 CHECK (regras_divisao <> '2' OR valor_a_onerar <= 100)
);

-- Snapshot das URs alcançadas (webhook / consulta / AP013)
CREATE TABLE contrato.garantia_ur (
  garantia_id             cerc.ulid NOT NULL,
  cnpj_credenciadora      cerc.documento NOT NULL,
  documento_ufr           cerc.documento NOT NULL,
  documento_titular       cerc.documento NOT NULL,
  codigo_arranjo          TEXT NOT NULL,
  data_liquidacao         DATE NOT NULL,
  origem                  TEXT NOT NULL CHECK (origem IN ('WEBHOOK','CONSULTA','AP013')),
  constituicao            TEXT NOT NULL CHECK (constituicao IN ('1','2')),
  valor_constituido_total cerc.valor_monetario,
  valor_bloqueado         cerc.valor_monetario,
  indicador_oneracao      TEXT NOT NULL,          -- '0' insucesso, '1'..'N' prioridade
  regras_divisao          TEXT,
  valor_onerado           NUMERIC(18,2),
  valor_constituido_efeito cerc.valor_monetario,
  snapshot_em             TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (garantia_id, cnpj_credenciadora, documento_ufr, documento_titular,
               codigo_arranjo, data_liquidacao, origem, snapshot_em)
) PARTITION BY RANGE (snapshot_em);

CREATE TABLE contrato.indicador_consistencia (
  contrato_id cerc.ulid NOT NULL REFERENCES contrato.contrato(id) ON DELETE CASCADE,
  indicador   TEXT NOT NULL,
  observado_em TIMESTAMPTZ NOT NULL,
  resultado   TEXT NOT NULL,
  parametros  JSONB,
  criticidade TEXT NOT NULL CHECK (criticidade IN ('0','1','2','3')),
  PRIMARY KEY (contrato_id, indicador, observado_em)
);

CREATE TABLE contrato.contrato_evento (
  id          BIGSERIAL,
  contrato_id cerc.ulid NOT NULL,
  tipo        TEXT NOT NULL,
  payload     JSONB NOT NULL,
  ocorrido_em TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (id, ocorrido_em)
) PARTITION BY RANGE (ocorrido_em);

CREATE TABLE contrato.simulacao_contrato (
  id                 cerc.ulid PRIMARY KEY,
  referencia_externa TEXT UNIQUE NOT NULL,
  request            JSONB NOT NULL,
  resultado          JSONB,
  criado_em          TIMESTAMPTZ NOT NULL DEFAULT now(),
  expira_em          TIMESTAMPTZ NOT NULL
);

CREATE TABLE contrato.divergencia_ap013 (
  id           BIGSERIAL PRIMARY KEY,
  arquivo      TEXT NOT NULL,
  leiaute      TEXT NOT NULL CHECK (leiaute IN ('AP013','AP013A','AP013B','AP013C')),
  contrato_id  cerc.ulid,
  campo        TEXT NOT NULL,
  valor_local  TEXT,
  valor_cerc   TEXT,
  detectada_em TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolvida_em TIMESTAMPTZ
);

CREATE INDEX ON contrato.contrato (status)
  WHERE status IN ('AGUARDANDO_WEBHOOK','PENDENTE_CONCILIACAO','ENVIANDO');
CREATE INDEX ON contrato.contrato (status_garantia)
  WHERE status_garantia IN ('INSUFICIENTE','EXCESSO');
CREATE INDEX ON contrato.contrato (cnpj_detentor, data_vencimento);
CREATE INDEX ON contrato.contrato (id_contrato_cerc) WHERE id_contrato_cerc IS NOT NULL;
CREATE INDEX ON contrato.garantia (contrato_id);
CREATE INDEX ON contrato.divergencia_ap013 (resolvida_em) WHERE resolvida_em IS NULL;
```

### 5.4 Schema `agenda` (SPEC 03) — o núcleo de volume

```sql
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
-- Suporte ao rate limit de consulta online (SPEC 03 §8.4)
CREATE INDEX ON agenda.consulta_agenda (filtro_ufr, iniciada_em)
  WHERE modo = 'ONLINE';

-- ---------------------------------------------------------------
-- Tabela dominante: projetada para bilhões de linhas
-- ---------------------------------------------------------------
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
```

> **Ordem da PK importa.** `data_liquidacao` vem **primeiro** porque é a chave de partição e o filtro mais seletivo das queries de posição. Em seguida vêm as dimensões de agrupamento. Essa ordem permite *index-only scan* na maioria das agregações de carteira.

```sql
-- Índice para "posição do UFR na janela" — a query mais frequente do produto
CREATE INDEX ON agenda.agenda_ur (documento_ufr, data_liquidacao)
  INCLUDE (valor_livre, valor_constituido_total, valor_bloqueado, constituicao);

-- Fumaça é consultada separadamente e é o maior volume — índice parcial
CREATE INDEX ON agenda.agenda_ur (documento_ufr, data_liquidacao)
  WHERE constituicao = '1';

-- Monitoramento de frescor
CREATE INDEX ON agenda.agenda_ur (data_hora_ultima_atualizacao);

-- ---------------------------------------------------------------
-- Pagamentos / efeitos por UR  (CORRIGIDO — ver §0)
-- ---------------------------------------------------------------
CREATE TABLE agenda.agenda_ur_pagamento (
  data_liquidacao        DATE NOT NULL,
  entidade_registradora  cerc.documento NOT NULL,
  cnpj_credenciadora     cerc.documento NOT NULL,
  documento_ufr          cerc.documento NOT NULL,
  documento_titular      cerc.documento NOT NULL,
  codigo_arranjo         TEXT NOT NULL,
  tipo_informacao_pagamento TEXT NOT NULL
    CHECK (tipo_informacao_pagamento IN ('1','2','3','4','5','6','7','8')),
  -- NOT NULL DEFAULT '' substitui o COALESCE inválido em PK
  indicador_efeitos_contrato TEXT NOT NULL DEFAULT '',
  identificador_cerc_contrato TEXT,          -- col. 12.16 → contrato.id_contrato_cerc
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

-- Junção com contrato (SPEC 02) — a razão de ser da coluna 12.16
CREATE INDEX ON agenda.agenda_ur_pagamento (identificador_cerc_contrato)
  WHERE identificador_cerc_contrato IS NOT NULL;

-- Efeitos de contrato são o subconjunto relevante para crédito
CREATE INDEX ON agenda.agenda_ur_pagamento (documento_ufr, data_liquidacao)
  WHERE tipo_informacao_pagamento IN ('1','2','3','4','8');

-- Vínculo N:N consulta ↔ UR
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

CREATE INDEX ON agenda.consulta_agenda_ur (consulta_id);

-- URs de webhook sem consulta correlacionável (SPEC 03 §5.4) — nunca descartar
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
```

### 5.5 Regra de upsert de `agenda_ur`

A mesma UR chega por três canais com frescor diferente. A regra (SPEC 03 §9) vira SQL:

```sql
INSERT INTO agenda.agenda_ur AS a (...)
VALUES (...)
ON CONFLICT (data_liquidacao, entidade_registradora, cnpj_credenciadora,
             documento_ufr, documento_titular, codigo_arranjo)
DO UPDATE SET
  constituicao = EXCLUDED.constituicao,
  valor_constituido_total = EXCLUDED.valor_constituido_total,
  valor_livre = EXCLUDED.valor_livre,
  valor_bloqueado = EXCLUDED.valor_bloqueado,
  valor_total_ur = EXCLUDED.valor_total_ur,
  data_hora_ultima_atualizacao = EXCLUDED.data_hora_ultima_atualizacao,
  origem = EXCLUDED.origem,
  atualizado_em = now()
WHERE EXCLUDED.data_hora_ultima_atualizacao > a.data_hora_ultima_atualizacao
   OR (EXCLUDED.data_hora_ultima_atualizacao = a.data_hora_ultima_atualizacao
       AND cerc.precedencia_origem(EXCLUDED.origem) > cerc.precedencia_origem(a.origem));
```

```sql
CREATE FUNCTION cerc.precedencia_origem(o TEXT) RETURNS INT
  IMMUTABLE LANGUAGE sql AS
$$ SELECT CASE o WHEN 'WEBHOOK' THEN 3 WHEN 'SINCRONO' THEN 2 ELSE 1 END $$;
```

> **A cláusula `WHERE` no `DO UPDATE` é o coração da correção do modelo.** Sem ela, um arquivo AP005 processado com atraso sobrescreve dados mais recentes vindos de webhook — e a posição de crédito volta no tempo silenciosamente. Não é otimização; é integridade.

**Ingestão em massa:** usar `COPY` para tabela temporária *unlogged*, deduplicar lá (`DISTINCT ON` pela chave, ordenando por `data_hora_ultima_atualizacao DESC`) e então um único `INSERT ... ON CONFLICT` a partir dela. Linha a linha não sustenta milhões de registros por janela.

---

## 6. Dimensionamento

### 6.1 Estimativa por linha

| Tabela | Bytes/linha (dados + índices) | Observação |
|---|---|---|
| `agenda_ur` | ~320 B | 6 colunas de documento dominam |
| `agenda_ur_pagamento` | ~450 B | `domicilio JSONB` pesa |
| `consulta_agenda_ur` | ~150 B | vida curta |
| `webhook_inbox` | ~2–8 KB | payload JSONB completo |

### 6.2 Cenário de referência (500M URs ativas)

```
agenda_ur              500M × 320 B   ≈  160 GB
agenda_ur_pagamento    750M × 450 B   ≈  340 GB   (1,5 pagamento/UR)
consulta_agenda_ur     rotativo 90d   ≈   20 GB
webhook_inbox          rotativo 90d   ≈   60 GB
demais schemas                        ≈   30 GB
                                        --------
                        total quente   ≈  610 GB
```

Com 13 meses quentes e expurgo funcionando, o cluster estabiliza na casa de **1 TB**. Sem particionamento e sem expurgo, cresce de forma monotônica e o `VACUUM` deixa de acompanhar por volta de 2–3 TB — o ponto em que um índice deixa de caber em memória e o p99 das consultas de posição sai de dezenas de ms para segundos.

### 6.3 Recomendações de infraestrutura

- **RAM:** mínimo 64 GB; `shared_buffers` 25 % da RAM; `effective_cache_size` 75 %.
- **Disco:** NVMe para partições quentes; `random_page_cost = 1.1`.
- **`work_mem`:** 64–128 MB nas sessões de agregação (não global — o ingestor com 16 workers multiplicaria isso).
- **`max_parallel_workers_per_gather` ≥ 4** para as agregações de posição.
- **Réplica de leitura** obrigatória: relatórios, dashboards e a exportação de compliance (§7.2) nunca no primário.

### 6.4 Autovacuum em tabelas de alto churn

`agenda_ur` recebe muito `UPDATE` (a UR muda de constituída para liquidada). Padrão do Postgres é conservador demais:

```sql
ALTER TABLE agenda.agenda_ur SET (
  autovacuum_vacuum_scale_factor = 0.02,
  autovacuum_analyze_scale_factor = 0.01,
  autovacuum_vacuum_cost_limit = 2000
);
```

Sem esse ajuste, o *bloat* cresce até o índice de posição perder a eficiência — sintoma típico: consulta que era rápida degrada ao longo de semanas sem mudança de código.

---

## 7. Retenção e ciclo de vida

### 7.1 Matriz de retenção

| Dado | Prazo | Base | Mecanismo |
|---|---|---|---|
| `optin`, `optout`, evidência de autorização | **5 anos** | regulatório (SPEC 01 R7) | não expurgar; arquivar após 2 anos |
| `cerc_requisicao` | **5 anos** | auditoria regulatória | `DROP PARTITION` após 60 meses |
| `consulta_agenda` (trilha de compliance) | **5 anos** | opt-in instantâneo fiscalizável (SPEC 03 §8) | não expurgar |
| `contrato` + `garantia` | vigência + 5 anos | regulatório | arquivar após baixa + 5 anos |
| `agenda_ur` / `agenda_ur_pagamento` | **13 meses quentes** | operacional | `DROP PARTITION`, com export prévio |
| `consulta_agenda_ur` | 90 dias | operacional | `DROP PARTITION` |
| `webhook_inbox` | 90 dias após processado | operacional | `DROP PARTITION` |
| `simulacao_contrato` | 30 dias | operacional | `DELETE` por `expira_em` |

> **Distinção que importa:** a obrigação de 5 anos recai sobre **a autorização e a trilha de acesso**, não sobre o dado de agenda em si. Isso permite manter `agenda_ur` enxuta sem risco regulatório — desde que `consulta_agenda` e `optin` fiquem intactas. Confirmar com o jurídico antes de habilitar o expurgo.

### 7.2 Arquivamento antes do `DROP PARTITION`

```
partição a expirar → export Parquet (S3/GCS) → verificação de contagem → DROP PARTITION
```

Nunca dropar sem verificar a contagem exportada contra `pg_class.reltuples`. O histórico de agenda é insumo de modelo de crédito; perdê-lo é perda de ativo, não de log.

### 7.3 Camada analítica

Acima de 500M URs, agregações históricas (safras, comportamento por credenciadora, séries de liquidação) não pertencem ao OLTP. Padrão recomendado: o Parquet exportado em §7.2 vira a camada fria consultável por DuckDB/Athena/BigQuery. O Postgres fica com os 13 meses que operam decisão em tempo real.

---

## 8. Acesso, isolamento e segurança

### 8.1 Roles

```sql
CREATE ROLE app_optin    LOGIN;
CREATE ROLE app_contrato LOGIN;
CREATE ROLE app_agenda   LOGIN;
CREATE ROLE app_leitura  LOGIN;   -- BI / relatórios, na réplica

GRANT USAGE ON SCHEMA cerc TO app_optin, app_contrato, app_agenda;
GRANT SELECT ON ALL TABLES IN SCHEMA cerc TO app_optin, app_contrato, app_agenda;
GRANT INSERT ON cerc.cerc_requisicao, cerc.webhook_inbox
  TO app_optin, app_contrato, app_agenda;

GRANT USAGE, CREATE ON SCHEMA optin    TO app_optin;
GRANT USAGE, CREATE ON SCHEMA contrato TO app_contrato;
GRANT USAGE, CREATE ON SCHEMA agenda   TO app_agenda;

-- Leitura cruzada apenas via view
GRANT SELECT ON optin.v_base_autorizativa TO app_agenda;
GRANT SELECT ON contrato.v_contrato_resumo TO app_agenda;
GRANT SELECT ON agenda.v_posicao_ufr TO app_contrato;
```

### 8.2 Views de fronteira (contrato entre serviços)

```sql
-- agenda-service consulta base autorizativa sem enxergar o agregado de opt-in
CREATE VIEW optin.v_base_autorizativa AS
SELECT o.id, o.documento_ufr, o.documento_titular,
       o.vigencia_inicio, o.vigencia_fim, o.origem,
       array_agg(DISTINCT c.cnpj) AS credenciadoras,
       array_agg(DISTINCT a.codigo) AS arranjos
FROM optin.optin o
LEFT JOIN optin.optin_credenciadora c ON c.optin_id = o.id
LEFT JOIN optin.optin_arranjo a       ON a.optin_id = o.id
WHERE o.status = 'ATIVO'
GROUP BY o.id;

-- contrato-service dimensiona garantia a partir da posição de agenda
CREATE VIEW agenda.v_posicao_ufr AS
SELECT documento_ufr, data_liquidacao, cnpj_credenciadora, codigo_arranjo,
       sum(valor_livre)             FILTER (WHERE constituicao = '1') AS valor_livre_constituido,
       sum(valor_constituido_total) FILTER (WHERE constituicao = '1') AS valor_constituido,
       sum(valor_constituido_total) FILTER (WHERE constituicao = '2') AS valor_fumaca,
       max(data_hora_ultima_atualizacao) AS frescor
FROM agenda.agenda_ur
GROUP BY 1,2,3,4;
```

> **Fumaça separada por `FILTER` na própria view.** Se a segregação depender de cada consumidor lembrar de filtrar, um dia alguém não lembra — e a decisão de crédito passa a contar recebível que não existe.

**Sem FK entre schemas.** `agenda_ur_pagamento.identificador_cerc_contrato` referencia `contrato.contrato.id_contrato_cerc` **por valor**, não por FK: os dados chegam por canais independentes e em ordem não garantida (a UR pode chegar antes do contrato ser confirmado). Uma FK aqui rejeitaria dado válido. A integridade é verificada por job de reconciliação, não pelo banco.

### 8.3 Dados pessoais (LGPD)

CPF/CNPJ de UFR e titular são dado pessoal. Medidas:

- **Criptografia at-rest** no volume (TDE do provedor gerenciado) — não criptografia por coluna: documentos são chave de junção e índice, e `pgcrypto` por coluna inviabilizaria ambos.
- **Sem PII em log.** Documentos mascarados na aplicação (`12345678****99`); íntegros só nas colunas.
- **Role de BI sem acesso a coluna de documento** — expor apenas via view com hash (`encode(sha256(documento::bytea),'hex')`) quando o caso de uso for contagem/agrupamento e não identificação.
- Direito de eliminação: **não se aplica** a dado com base legal de obrigação regulatória (retenção de 5 anos). Documentar isso formalmente antes que a primeira solicitação chegue.

---

## 9. Evolução do schema

1. **Toda mudança é uma migration versionada**, aplicada por ferramenta (Flyway/Liquibase), nunca DDL manual em produção.
2. **Expand → migrate → contract.** Coluna nova sempre `NULL`-able primeiro; backfill em lotes; `NOT NULL` depois, via `ADD CONSTRAINT ... NOT VALID` + `VALIDATE CONSTRAINT` (evita lock de tabela inteira).
3. **Nunca `ALTER TABLE ... ADD COLUMN ... DEFAULT <volátil>`** em tabela particionada grande — reescreve tudo.
4. **Domínios da CERC mudam sem aviso longo.** O caso real: a coluna 12.16 do AP005 saiu de inexistente para obrigatória em produção em 3 meses. O modelo já a contempla, mas o padrão vai se repetir — por isso `TEXT` + `CHECK` (§3.4) e tabela de domínio para arranjos.
5. **Índice novo sempre `CREATE INDEX CONCURRENTLY`.** Em tabela particionada, criar em cada partição e depois `ATTACH` no índice pai.

---

## 10. Checklist de implantação

- [ ] Quatro schemas criados na ordem `cerc → optin → contrato → agenda`
- [ ] `cerc.dominio_arranjo` semeado com o domínio v1.5 + `99T`
- [ ] `cerc.participante_slc` populado (bloqueia domicílio fora do SLC)
- [ ] `pg_partman` (ou job equivalente) configurado, com **24 meses de partições futuras** pré-criadas
- [ ] Partição `DEFAULT` criada em cada tabela particionada **e monitorada** (contagem > 0 = alerta)
- [ ] Índice GiST de `daterange` em `optin.optin` criado — sem ele a validação de base autorizativa não escala
- [ ] Upsert de `agenda_ur` com a cláusula `WHERE` de frescor (§5.5) testado com dado fora de ordem
- [ ] Autovacuum ajustado em `agenda_ur` e `agenda_ur_pagamento`
- [ ] Roles por serviço, sem `SUPERUSER`, sem acesso cruzado fora das views
- [ ] Réplica de leitura ativa e apontada para BI/relatórios
- [ ] Rotina de export Parquet validada **antes** de habilitar qualquer `DROP PARTITION`
- [ ] Verificação de CI: nenhuma coluna monetária fora de `NUMERIC`
- [ ] Retenção de 5 anos confirmada com jurídico, com a distinção do §7.1 documentada
- [ ] Plano de restore testado (não apenas backup — restore cronometrado)

---

## 11. Riscos conhecidos do modelo

1. **`agenda_ur` sem `id` técnico.** A PK é composta de 6 colunas. Isso é deliberado (a chave natural é a identidade real da UR e evita uma coluna de 26 bytes × bilhões de linhas), mas torna FKs a partir dela impraticáveis — daí `agenda_ur_pagamento` repetir a chave em vez de referenciar. Aceito conscientemente.
2. **`domicilio` como `JSONB`.** Escolhido porque a estrutura difere entre canais (`CG`/`CI` só na agenda) e não é alvo de filtro. Se virar critério de busca, promover para colunas.
3. **Correlação consulta ↔ UR é heurística** (SPEC 03 §5.4). `consulta_agenda_ur` materializa o resultado dessa heurística; se a CERC vier a expor um id de consulta no webhook, essa tabela simplifica bastante.
4. **`tipo_efeito = '8'` aceito na escrita do banco.** Enquanto a pendência da SPEC 02 §12.1 não for resolvida, o banco é mais permissivo que a API de envio — intencional, mas exige que a validação de aplicação não seja removida por engano.
5. **Volume de `agenda_ur_pagamento` pode surpreender.** A estimativa de 1,5 pagamento por UR vale para carteira com pouca oneração múltipla. Em cenário de ônus empilhados (até 45 efeitos por UR, SPEC 02), pode chegar a 5–10× a projeção. Instrumentar desde o primeiro mês.
