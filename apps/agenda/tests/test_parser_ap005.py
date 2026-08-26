from datetime import date

import pytest

from apps.agenda import parser_ap005


def test_parse_nome_arquivo_ap005():
    meta = parser_ap005.parse_nome_arquivo("CERC-AP005_53462828_20190221_0000001_ret.csv")
    assert meta == {
        "tipo_leiaute": "CERC-AP005", "ident_ic": "53462828",
        "data_req": date(2019, 2, 21), "seq": 1, "comprimido": False,
    }


def test_parse_nome_arquivo_ap005a_comprimido():
    meta = parser_ap005.parse_nome_arquivo("CERC-AP005A_53462828_20190221_0000042_ret.csv.gz")
    assert meta["tipo_leiaute"] == "CERC-AP005A"
    assert meta["seq"] == 42
    assert meta["comprimido"] is True


def test_parse_nome_arquivo_invalido():
    with pytest.raises(parser_ap005.NomeArquivoInvalidoError):
        parser_ap005.parse_nome_arquivo("arquivo_qualquer.csv")


@pytest.mark.parametrize("n,layout", [
    (2, "REDUZIDO"), (4, "REDUZIDO"), (15, "SEM_PAGAMENTO"),
    (30, "COM_PAGAMENTO_SEM_1216"), (31, "COM_PAGAMENTO_COM_1216"),
])
def test_detectar_layout(n, layout):
    assert parser_ap005.detectar_layout(n) == layout


def test_detectar_layout_contagem_inesperada():
    with pytest.raises(parser_ap005.LinhaInvalidaError):
        parser_ap005.detectar_layout(20)


def _linha_completa(*, com_1216: bool, tipo_informacao_pagamento="6", indicador_efeitos_contrato=""):
    base = [
        "REF-001", "22246686000196", "01027058000191", "12345678000199", "VCC",
        "2026-09-15", "12345678000199", "1", "1000.00", "0", "0",
    ]
    bloco_12 = [
        "12345678000199", "CC", "001", "00000000", "1234", "123456-7",
        "500.00", "", "", "", "", "", tipo_informacao_pagamento, indicador_efeitos_contrato, "",
    ]
    if com_1216:
        bloco_12.append("CTR-123")
    cauda = ["", "0", "1000.00", "2026-09-15T10:00:00Z"]
    return base + bloco_12 + cauda


def test_traduzir_linha_reduzida_retorna_none():
    assert parser_ap005.traduzir_linha(["REF-001", "2026-09-15T10:00:00Z"], "CERC-AP005") is None


def test_traduzir_linha_sem_pagamento():
    campos = [
        "REF-001", "22246686000196", "01027058000191", "12345678000199", "VCC",
        "2026-09-15", "12345678000199", "2", "1000.00", "0", "0",
        "", "0", "1000.00", "2026-09-15T10:00:00Z",
    ]
    cabecalho, pagamento = parser_ap005.traduzir_linha(campos, "CERC-AP005B")
    assert pagamento is None
    assert cabecalho["documento_titular"] == "12345678000199"
    assert cabecalho["constituicao"] == "2"
    assert cabecalho["origem"] == "ARQUIVO"
    assert cabecalho["origem_arquivo"] == "CERC-AP005B"
    assert cabecalho["valor_total_ur"] == 1000.00


def test_traduzir_linha_com_pagamento_e_12_16():
    campos = _linha_completa(com_1216=True)
    cabecalho, pagamento = parser_ap005.traduzir_linha(campos, "CERC-AP005")
    assert cabecalho["entidade_registradora"] == "22246686000196"
    assert pagamento["tipo_informacao_pagamento"] == "6"
    assert pagamento["identificador_cerc_contrato"] == "CTR-123"
    assert pagamento["indicador_efeitos_contrato"] == ""
    assert pagamento["domicilio"]["ispb"] == "00000000"
    assert pagamento["valor_a_pagar"] == 500.00


def test_traduzir_linha_sem_coluna_12_16():
    campos = _linha_completa(com_1216=False)
    _, pagamento = parser_ap005.traduzir_linha(campos, "CERC-AP005")
    assert pagamento["identificador_cerc_contrato"] is None


def test_traduzir_linha_tipo_informacao_pagamento_invalido():
    campos = _linha_completa(com_1216=True, tipo_informacao_pagamento="9")
    with pytest.raises(parser_ap005.LinhaInvalidaError):
        parser_ap005.traduzir_linha(campos, "CERC-AP005")


def test_traduzir_linha_campo_obrigatorio_vazio():
    campos = _linha_completa(com_1216=True)
    campos[1] = ""  # entidade_registradora vazio
    with pytest.raises(parser_ap005.LinhaInvalidaError):
        parser_ap005.traduzir_linha(campos, "CERC-AP005")


def test_traduzir_linha_indicador_efeitos_contrato_vazio_vira_string_vazia():
    campos = _linha_completa(com_1216=True, indicador_efeitos_contrato="")
    _, pagamento = parser_ap005.traduzir_linha(campos, "CERC-AP005")
    assert pagamento["indicador_efeitos_contrato"] == ""
    assert pagamento["indicador_efeitos_contrato"] is not None
