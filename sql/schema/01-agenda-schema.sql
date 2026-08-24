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
