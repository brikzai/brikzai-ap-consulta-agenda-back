"""Ingestão de arquivo AP005/AP005A/AP005B (design doc §9, SPEC03 §6.6).

importar_arquivo(financiador_id, nome_arquivo, conteudo) é o ponto de
entrada — conteudo é um objeto binário lido em streaming, nunca
materializado inteiro em memória (SPEC03 §6.6). Não basta um `.read()`
solto: `_abrir_texto` empacota `conteudo` (ou o `gzip.GzipFile` em cima
dele) num `io.TextIOWrapper`, que exige o protocolo de leitura binária
completo (`readable()` retornando `True` e leitura via `readinto()`/
`read(n)`, ao estilo `io.RawIOBase`) — ver `apps/agenda/views.py`,
`_StreamDeRequisicao`, que implementa esse contrato para expor
`request` (que só tem `.read(size)`) como um binário compatível.
AP005A/AP005B chegam em .gzip (§6.5), descompactados por streaming via
gzip.GzipFile.

Idempotência por (tipo_leiaute, ident_ic, data_req, seq) — SPEC03 §6.6: se
já existe uma linha em arquivo_agenda_processado (de QUALQUER tentativa
anterior, mesmo incompleta), o arquivo é considerado já reivindicado e a
chamada é um no-op. Reprocessar um arquivo travado a meio caminho exige
apagar manualmente a linha de controle — gap aceito conscientemente (mesma
filosofia de "não resolver antes de medir" do design doc §15).

Agrupamento por UR: o arquivo real da CERC repete as colunas 1-11/13-16 a
cada linha de pagamento da mesma UR (Plano 08, ver parser_ap005.py) — este
módulo assume que essas linhas vêm CONSECUTIVAS no arquivo (a mesma chave
não reaparece depois que outra chave começou) para poder agrupar em
streaming, sem materializar o arquivo inteiro. O loop principal NUNCA força
um flush do grupo em andamento por causa de uma linha rejeitada
(LinhaInvalidaError) ou do leiaute reduzido (resultado None) no meio das
linhas de uma mesma UR — só uma mudança de chave (`_Agrupador.adicionar`)
ou o fim do arquivo (`agrupador.finalizar()`, uma vez, após o loop) fecham
um grupo; do contrário, uma linha rejeitada ou reduzida entre dois
pagamentos da mesma UR cindiria o grupo em dois `upsert_agenda_ur`, e o
segundo silenciosamente não gravaria nada (ver o parágrafo seguinte) —
achado 1 da revisão final do Plano 08. Se o arquivo real não vier agrupado
de forma consecutiva (mesma chave reaparecendo depois de outra ter
começado), cada grupo não-consecutivo é tratado como upsert independente,
e o resultado depende do timestamp: como `data_hora_ultima_atualizacao`
(coluna 16) repete o MESMO valor em todas as linhas de uma UR dentro de um
único arquivo, dois grupos não-consecutivos da mesma UR chegam a
`upsert_agenda_ur` com timestamps IGUAIS — o desempate de
`repository._deve_sobrescrever` (`precedencia_origem` só quebra o empate
quando as origens diferem; aqui as duas chamadas são `origem="ARQUIVO"`)
faz a SEGUNDA chamada retornar `sobrescrito=False` e não gravar nada: é o
grupo mais TARDIO que se perde, não o mais antigo. Só quando o segundo
grupo chega com um timestamp estritamente mais novo (ou origem de maior
precedência) é que a chamada prossegue e `_limpar_pagamentos_obsoletos`
remove os pagamentos do grupo anterior que o novo lote não repetir — esse
caso (não o do timestamp empatado, que é o comum dentro de um mesmo
arquivo) é a mesma classe de gap best-effort do design doc §15 risco 1,
carregado explicitamente aqui até haver um arquivo real para confirmar a
ordenação (SPEC03 §13).
"""
import csv
import gzip
import io
import logging
from datetime import datetime, timezone

from apps.agenda import parser_ap005
from apps.agenda.repository import _CHAVE_UR, upsert_agenda_ur
from shared.cloudsql_client import get_db

logger = logging.getLogger(__name__)

_LIMIAR_ALERTA_REJEICAO = 0.005


def _ja_processado(db, meta: dict) -> bool:
    existentes = (
        db.table("arquivo_agenda_processado").select("tipo_leiaute")
        .eq("tipo_leiaute", meta["tipo_leiaute"]).eq("ident_ic", meta["ident_ic"])
        .eq("data_req", meta["data_req"]).eq("seq", meta["seq"]).execute()
    )
    return bool(existentes.data)


def _registrar_rejeitada(db, nome_arquivo: str, numero_linha: int, campos: list, motivo: str) -> None:
    db.table("agenda_ur_rejeitada").insert({
        "origem": "ARQUIVO",
        "arquivo": nome_arquivo,
        "linha": numero_linha,
        "conteudo": ",".join(campos),
        "motivo": motivo,
    }).execute()


def _abrir_texto(conteudo, comprimido: bool):
    stream = gzip.GzipFile(fileobj=conteudo, mode="rb") if comprimido else conteudo
    return io.TextIOWrapper(stream, encoding="utf-8", errors="replace", newline="")


class _Agrupador:
    """Acumula linhas consecutivas da mesma chave de UR e dispara
    upsert_agenda_ur uma vez por grupo, nunca uma vez por linha — o
    contrato de upsert_agenda_ur exige a lista COMPLETA de pagamentos da
    UR no lote (repository.py, docstring do módulo)."""

    def __init__(self, financiador_id: str, nome_arquivo: str, db):
        self._financiador_id = financiador_id
        self._nome_arquivo = nome_arquivo
        self._db = db
        self._chave = None
        self._cabecalho = None
        self._linhas_do_grupo: list = []  # [(numero_linha, campos_brutos, pagamento_ou_none)]
        self.linhas_ok = 0
        self.linhas_rejeitadas = 0

    def adicionar(self, numero_linha: int, campos: list, cabecalho: dict, pagamento) -> None:
        chave = tuple(cabecalho[campo] for campo in _CHAVE_UR)
        if self._chave is not None and chave != self._chave:
            self._flush()
        self._chave = chave
        self._cabecalho = cabecalho
        self._linhas_do_grupo.append((numero_linha, campos, pagamento))

    def _flush(self) -> None:
        if self._cabecalho is None:
            return
        pagamentos = [p for (_, _, p) in self._linhas_do_grupo if p is not None]
        try:
            upsert_agenda_ur(self._financiador_id, self._cabecalho, pagamentos)
            self.linhas_ok += len(self._linhas_do_grupo)
        except Exception as exc:
            logger.exception(
                "[ImportarAP005] Falha ao gravar UR (financiador=%s, arquivo=%s, linhas=%s)",
                self._financiador_id, self._nome_arquivo, [n for (n, _, _) in self._linhas_do_grupo],
            )
            for numero_linha, campos, _ in self._linhas_do_grupo:
                _registrar_rejeitada(self._db, self._nome_arquivo, numero_linha, campos, f"{type(exc).__name__}: {exc}")
            self.linhas_rejeitadas += len(self._linhas_do_grupo)
        self._chave = None
        self._cabecalho = None
        self._linhas_do_grupo = []

    def finalizar(self) -> None:
        self._flush()


def importar_arquivo(financiador_id: str, nome_arquivo: str, conteudo) -> dict:
    meta = parser_ap005.parse_nome_arquivo(nome_arquivo)
    db = get_db(financiador_id)

    if _ja_processado(db, meta):
        logger.info("[ImportarAP005] Arquivo já processado, ignorando (arquivo=%s)", nome_arquivo)
        return {"ja_processado": True, **meta}

    db.table("arquivo_agenda_processado").insert({
        "tipo_leiaute": meta["tipo_leiaute"], "ident_ic": meta["ident_ic"],
        "data_req": meta["data_req"], "seq": meta["seq"],
        "iniciado_em": datetime.now(timezone.utc),
    }).execute()

    texto = _abrir_texto(conteudo, meta["comprimido"])
    agrupador = _Agrupador(financiador_id, nome_arquivo, db)
    linhas_lidas = 0
    linhas_sem_agenda = 0
    linhas_rejeitadas_parser = 0

    for numero_linha, campos in enumerate(csv.reader(texto), start=1):
        linhas_lidas += 1
        try:
            resultado = parser_ap005.traduzir_linha(campos, meta["tipo_leiaute"])
        except parser_ap005.LinhaInvalidaError as exc:
            _registrar_rejeitada(db, nome_arquivo, numero_linha, campos, str(exc))
            linhas_rejeitadas_parser += 1
            continue

        if resultado is None:
            if len(campos) >= 3:
                # SPEC03 §6.2: o leiaute reduzido "sem agenda" carrega uma
                # lista de erros (código + descrição) quando a CERC reportou
                # erro para essa linha — o parser não expõe esses campos
                # (contrato de retorno não muda, Finding 3 da revisão final
                # do Plano 08), então isso é só um log de observabilidade;
                # a linha continua contando como OK, nunca rejeitada.
                logger.warning(
                    "[ImportarAP005] Linha em leiaute reduzido com possível lista de erros da CERC (arquivo=%s, linha=%d, conteudo=%s)",
                    nome_arquivo, numero_linha, ",".join(campos),
                )
            linhas_sem_agenda += 1
            continue

        cabecalho, pagamento = resultado
        agrupador.adicionar(numero_linha, campos, cabecalho, pagamento)

    agrupador.finalizar()

    linhas_ok = agrupador.linhas_ok + linhas_sem_agenda
    linhas_rejeitadas = agrupador.linhas_rejeitadas + linhas_rejeitadas_parser

    if linhas_lidas and linhas_rejeitadas / linhas_lidas > _LIMIAR_ALERTA_REJEICAO:
        logger.warning(
            "[ImportarAP005] Taxa de rejeição acima de %.1f%% (arquivo=%s, rejeitadas=%d, lidas=%d)",
            _LIMIAR_ALERTA_REJEICAO * 100, nome_arquivo, linhas_rejeitadas, linhas_lidas,
        )

    (
        db.table("arquivo_agenda_processado")
        .update({
            "linhas_lidas": linhas_lidas, "linhas_ok": linhas_ok, "linhas_rejeitadas": linhas_rejeitadas,
            "concluido_em": datetime.now(timezone.utc),
        })
        .eq("tipo_leiaute", meta["tipo_leiaute"]).eq("ident_ic", meta["ident_ic"])
        .eq("data_req", meta["data_req"]).eq("seq", meta["seq"])
        .execute()
    )

    return {
        "ja_processado": False, "linhas_lidas": linhas_lidas, "linhas_ok": linhas_ok,
        "linhas_rejeitadas": linhas_rejeitadas, **meta,
    }
