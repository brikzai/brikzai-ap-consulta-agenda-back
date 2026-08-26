import csv
import gzip
import io
from datetime import date

import pytest

from apps.agenda import importar_ap005
from apps.agenda.parser_ap005 import parse_nome_arquivo
from apps.agenda.repository import _CHAVE_UR, _com_filtros
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
NOME_ARQUIVO = "CERC-AP005_11112222_20260920_0000001_ret.csv"
NOME_ARQUIVO_GZ = "CERC-AP005A_11112222_20260921_0000002_ret.csv.gz"

_CHAVE_TESTE = {
    "data_liquidacao": date(2026, 9, 20),
    "entidade_registradora": "22246686000196",
    "cnpj_credenciadora": "01027058000191",
    "documento_ufr": "77777777000177",
    "documento_titular": "77777777000177",
    "codigo_arranjo": "VCC",
}


def _linha(*, tipo_informacao_pagamento="6", com_1216=True, identificador="CTR-TESTE", documento_titular=None):
    titular = documento_titular or _CHAVE_TESTE["documento_titular"]
    base = [
        "REF-001", _CHAVE_TESTE["entidade_registradora"], _CHAVE_TESTE["cnpj_credenciadora"],
        _CHAVE_TESTE["documento_ufr"], _CHAVE_TESTE["codigo_arranjo"], "2026-09-20",
        titular, "1", "1000.00", "0", "0",
    ]
    bloco = [
        titular, "CC", "001", "00000000", "1234", "123456-7",
        "500.00", "", "", "", "", "", tipo_informacao_pagamento, "", "",
    ]
    if com_1216:
        bloco.append(identificador)
    cauda = ["", "0", "1000.00", "2026-09-20T10:00:00Z"]
    return base + bloco + cauda


def _csv_bytes(linhas, *, comprimido=False) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer).writerows(linhas)
    dados = buffer.getvalue().encode("utf-8")
    if not comprimido:
        return dados
    saida = io.BytesIO()
    with gzip.GzipFile(fileobj=saida, mode="wb") as f:
        f.write(dados)
    return saida.getvalue()


def _apagar_meta(nome_arquivo):
    meta = parse_nome_arquivo(nome_arquivo)
    get_db(FINANCIADOR_TESTE).table("arquivo_agenda_processado").delete().eq(
        "tipo_leiaute", meta["tipo_leiaute"]).eq("ident_ic", meta["ident_ic"]
    ).eq("data_req", meta["data_req"]).eq("seq", meta["seq"]).execute()


@pytest.fixture(autouse=True)
def _limpar():
    db = get_db(FINANCIADOR_TESTE)

    def _fazer():
        _com_filtros(db.table("agenda_ur_pagamento").delete(), _CHAVE_TESTE, _CHAVE_UR).execute()
        _com_filtros(db.table("agenda_ur").delete(), _CHAVE_TESTE, _CHAVE_UR).execute()
        db.table("agenda_ur_rejeitada").delete().eq("arquivo", NOME_ARQUIVO).execute()
        _apagar_meta(NOME_ARQUIVO)
        _apagar_meta(NOME_ARQUIVO_GZ)

    _fazer()
    yield
    _fazer()


def test_importa_um_pagamento():
    conteudo = io.BytesIO(_csv_bytes([_linha()]))
    resultado = importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO, conteudo)

    assert resultado["ja_processado"] is False
    assert resultado["linhas_lidas"] == 1
    assert resultado["linhas_ok"] == 1
    assert resultado["linhas_rejeitadas"] == 0

    db = get_db(FINANCIADOR_TESTE)
    ur = _com_filtros(db.table("agenda_ur").select("*"), _CHAVE_TESTE, _CHAVE_UR).execute().data[0]
    assert ur["origem"] == "ARQUIVO"
    assert ur["origem_arquivo"] == "CERC-AP005"
    pagamentos = _com_filtros(db.table("agenda_ur_pagamento").select("*"), _CHAVE_TESTE, _CHAVE_UR).execute().data
    assert len(pagamentos) == 1
    assert pagamentos[0]["identificador_cerc_contrato"] == "CTR-TESTE"


def test_agrupa_linhas_consecutivas_da_mesma_ur_num_unico_upsert():
    linhas = [
        _linha(tipo_informacao_pagamento="6", identificador="CTR-1"),
        _linha(tipo_informacao_pagamento="1", identificador="CTR-2"),
    ]
    conteudo = io.BytesIO(_csv_bytes(linhas))
    resultado = importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO, conteudo)

    assert resultado["linhas_ok"] == 2
    db = get_db(FINANCIADOR_TESTE)
    pagamentos = _com_filtros(db.table("agenda_ur_pagamento").select("*"), _CHAVE_TESTE, _CHAVE_UR).execute().data
    assert {p["tipo_informacao_pagamento"] for p in pagamentos} == {"6", "1"}


def test_linha_invalida_no_meio_do_grupo_nao_quebra_upsert_da_ur():
    """Regressão (Plano 08, revisão final, achado 1): uma linha rejeitada
    NO MEIO das linhas de pagamento consecutivas da MESMA UR não pode
    quebrar o grupo em dois upserts — o segundo silenciosamente não
    grava nada (mesma data_hora_ultima_atualizacao, tie-break de origem
    igual em repository._deve_sobrescrever), perdendo o pagamento da
    linha válida que vem depois da rejeitada."""
    linhas = [
        _linha(tipo_informacao_pagamento="6", identificador="CTR-1"),
        _linha(tipo_informacao_pagamento="9", identificador="CTR-INVALIDA"),  # tipoInformacaoPagamento fora do domínio 1-8
        _linha(tipo_informacao_pagamento="1", identificador="CTR-2"),
    ]
    conteudo = io.BytesIO(_csv_bytes(linhas))
    resultado = importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO, conteudo)

    assert resultado["linhas_lidas"] == 3
    assert resultado["linhas_rejeitadas"] == 1
    assert resultado["linhas_ok"] == 2

    db = get_db(FINANCIADOR_TESTE)
    pagamentos = _com_filtros(db.table("agenda_ur_pagamento").select("*"), _CHAVE_TESTE, _CHAVE_UR).execute().data
    assert {p["tipo_informacao_pagamento"] for p in pagamentos} == {"6", "1"}


def test_idempotente_segunda_chamada_nao_reprocessa():
    conteudo1 = io.BytesIO(_csv_bytes([_linha()]))
    importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO, conteudo1)

    conteudo2 = io.BytesIO(_csv_bytes([_linha(identificador="CTR-DIFERENTE")]))
    resultado = importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO, conteudo2)

    assert resultado["ja_processado"] is True
    db = get_db(FINANCIADOR_TESTE)
    pagamentos = _com_filtros(db.table("agenda_ur_pagamento").select("*"), _CHAVE_TESTE, _CHAVE_UR).execute().data
    assert pagamentos[0]["identificador_cerc_contrato"] == "CTR-TESTE"


def test_linha_invalida_vai_para_rejeitada_e_arquivo_continua():
    linha_invalida = _linha(tipo_informacao_pagamento="9")
    linha_valida = _linha(documento_titular="88888888000188")
    conteudo = io.BytesIO(_csv_bytes([linha_invalida, linha_valida]))

    resultado = importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO, conteudo)

    assert resultado["linhas_lidas"] == 2
    assert resultado["linhas_rejeitadas"] == 1
    assert resultado["linhas_ok"] == 1

    db = get_db(FINANCIADOR_TESTE)
    rejeitadas = db.table("agenda_ur_rejeitada").select("*").eq("arquivo", NOME_ARQUIVO).execute().data
    assert len(rejeitadas) == 1
    assert rejeitadas[0]["linha"] == 1
    assert "tipoInformacaoPagamento" in rejeitadas[0]["motivo"]

    chave_valida = {**_CHAVE_TESTE, "documento_titular": "88888888000188"}
    ur = _com_filtros(db.table("agenda_ur").select("*"), chave_valida, _CHAVE_UR).execute().data
    assert len(ur) == 1
    _com_filtros(db.table("agenda_ur_pagamento").delete(), chave_valida, _CHAVE_UR).execute()
    _com_filtros(db.table("agenda_ur").delete(), chave_valida, _CHAVE_UR).execute()


def test_layout_reduzido_conta_como_linha_ok_sem_gravar_ur():
    conteudo = io.BytesIO(_csv_bytes([["REF-001", "2026-09-20T10:00:00Z"]]))
    resultado = importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO, conteudo)
    assert resultado["linhas_ok"] == 1
    assert resultado["linhas_rejeitadas"] == 0


def test_descompacta_gzip_em_streaming():
    conteudo = io.BytesIO(_csv_bytes([_linha()], comprimido=True))
    resultado = importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO_GZ, conteudo)
    assert resultado["linhas_ok"] == 1
    assert resultado["tipo_leiaute"] == "CERC-AP005A"
