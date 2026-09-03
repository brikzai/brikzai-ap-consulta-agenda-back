"""Testes puros dos tradutores de payload da CERC (sem banco/HTTP) — cobrem
especificamente a correção de SPEC-04 §1 (dinheiro é Decimal na aplicação,
nunca float) nas duas funções que constroem cabecalho/pagamento a partir da
resposta de POST /v15/agenda/consultar."""
from decimal import Decimal

from services.cerc.client import _traduzir_pagamento, _traduzir_ur


def _agenda_base(**overrides):
    base = {
        "entidadeRegistradora": "22246686000196",
        "instituicaoCredenciadora": "36216798000150",
        "codigoArranjoPagamento": "VCC",
        "documentoUsuarioFinalRecebedor": "12345678000199",
    }
    base.update(overrides)
    return base


def _ur_base(**overrides):
    base = {
        "dataLiquidacao": "2026-09-20",
        "constituicao": "1",
        "valorTotalUR": 1000.33,
        "carteira": None,
        "titulares": [{
            "documentoTitular": "12345678000199",
            "valorConstituidoTotal": 1000.33,
            "valorConstituidoAntecipacaoPre": 0,
            "valorBloqueado": 0,
            "valorLivre": 1000.33,
            "dataHoraUltimaAtualizacao": "2026-09-20T10:00:00Z",
            "pagamentos": [],
        }],
    }
    base.update(overrides)
    return base


def test_traduzir_ur_valores_monetarios_sao_decimal_nao_float():
    linhas = _traduzir_ur(_agenda_base(), _ur_base())
    cabecalho, _pagamentos = linhas[0]

    assert isinstance(cabecalho["valor_constituido_total"], Decimal)
    assert isinstance(cabecalho["valor_constituido_antecipacao_pre"], Decimal)
    assert isinstance(cabecalho["valor_bloqueado"], Decimal)
    assert isinstance(cabecalho["valor_livre"], Decimal)
    assert isinstance(cabecalho["valor_total_ur"], Decimal)
    assert cabecalho["valor_constituido_total"] == Decimal("1000.33")


def test_traduzir_pagamento_valores_monetarios_sao_decimal_nao_float():
    pagamento = _traduzir_pagamento({
        "tipoInformacaoPagamento": 6,
        "valorAPagar": 250.75,
        "valorOnerado": None,
        "valorConstituidoEfeito": None,
        "valorLiquidacaoEfetiva": None,
    })

    assert isinstance(pagamento["valor_a_pagar"], Decimal)
    assert pagamento["valor_a_pagar"] == Decimal("250.75")
    assert pagamento["valor_onerado"] is None  # campo opcional ausente segue None, não é convertido
