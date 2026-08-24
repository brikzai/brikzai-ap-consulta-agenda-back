-- Correções levantadas pela revisão final do Plano 02, aplicadas enquanto
-- o banco ainda está vazio (mais barato agora do que depois que existirem
-- dados reais):
--
-- 1. Índices que faltavam nos 2 caminhos de maior volume esperado
--    (recuperação de webhook_inbox, busca por correlacao_id).
-- 2. Índice redundante removido (consulta_id já é o primeiro campo da PK
--    de consulta_agenda_ur).
-- 3. Índice de status trocado por parcial (só PARCIAL importa pro job de
--    completude do design doc §8).
-- 4. Índice de rate-limit reordenado pra (filtro_ufr, modo, iniciada_em) —
--    igualdade antes de intervalo, é a ordem certa pra esse predicado.
-- 5. Índice parcial pra órfãos ainda não resolvidos (agenda_ur_orfa).
-- 6. Tabela de controle do que já foi aplicado, usada por apply_schema.py
--    a partir de agora pra não reaplicar por engano.

CREATE INDEX ON webhook_inbox (recebido_em) WHERE processado_em IS NULL;
CREATE INDEX ON cerc_requisicao (correlacao_id);

DROP INDEX consulta_agenda_ur_consulta_id_idx;

DROP INDEX consulta_agenda_status_idx;
CREATE INDEX ON consulta_agenda (status) WHERE status = 'PARCIAL';

DROP INDEX consulta_agenda_filtro_ufr_iniciada_em_modo_idx;
CREATE INDEX ON consulta_agenda (filtro_ufr, modo, iniciada_em);

CREATE INDEX ON agenda_ur_orfa (recebida_em) WHERE resolvida_em IS NULL;

CREATE TABLE schema_aplicado (
  arquivo       TEXT PRIMARY KEY,
  checksum      TEXT NOT NULL,
  aplicado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);
