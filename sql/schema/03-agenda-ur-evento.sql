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
