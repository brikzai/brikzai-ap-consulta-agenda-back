-- Coluna de cursor para paginação de GET /api/v1/agendas/urs (Plano 10,
-- design doc §10/§16): agenda_ur não tem coluna monotônica única (a chave
-- primária é composta de 6 colunas), então uma BIGSERIAL dedicada vira o
-- cursor de paginação — populada só no INSERT (nunca tocada pelo UPDATE
-- de upsert de frescor em apps/agenda/repository.py::_upsert_cabecalho),
-- permanecendo estável entre chamadas paginadas mesmo quando uma UR é
-- atualizada em lugar.

ALTER TABLE agenda_ur ADD COLUMN sequencia BIGSERIAL;
CREATE INDEX ON agenda_ur (sequencia);
