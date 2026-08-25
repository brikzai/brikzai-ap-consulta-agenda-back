-- Discriminador de efeito de pagamento em agenda_ur_evento (achado da
-- revisão final do Plano 05): sem isso, dois eventos LIQUIDACAO da mesma
-- UR (dois efeitos de pagamento liquidados em momentos diferentes, ou no
-- mesmo lote) ficam indistinguíveis exceto pelo id. NULL nos eventos que
-- não são por-linha-de-pagamento (CAPTURA, BLOQUEIO, DISPONIBILIZACAO).

ALTER TABLE agenda_ur_evento
  ADD COLUMN tipo_informacao_pagamento TEXT,
  ADD COLUMN indicador_efeitos_contrato TEXT NOT NULL DEFAULT '';
