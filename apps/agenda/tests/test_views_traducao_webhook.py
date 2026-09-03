"""Testes puros dos tradutores de payload do webhook (sem banco) — cobrem a
correção de SPEC-04 §1 (dinheiro é Decimal na aplicação, nunca float) nas
duas funções que constroem cabecalho/pagamento a partir do envelope
`tipoEvento=agenda` do webhook CERC. Não usa a fixture de banco de nenhum
outro módulo de teste desta pasta — só importa e chama funções puras."""
from decimal import Decimal

from apps.agenda.views import _traduzir_evento_webhook, _traduzir_pagamento_webhook


def _evento_base(**overrides):
    base = {
        "entidadeRegistradora": "22246686000196",
        "instituicaoCredenciadora": "36216798000150",
        "codigoArranjoPagamento": "VCC",
        "documentoUsuarioFinalRecebedor": "12345678000199",
        "documentoTitular": "12345678000199",
        "dataLiquidacao": "2026-09-20",
        "constituicao": "1",
        "valorConstituidoTotal": 1000.33,
        "valorConstituidoAntecipacaoPre": 0,
        "valorBloqueado": 0,
        "valorLivre": 1000.33,
        "valorTotalUR": 1000.33,
        "carteira": None,
        "dataHoraUltimaAtualizacao": "2026-09-20T10:00:00Z",
        "pagamentos": [],
    }
    base.update(overrides)
    return base


def test_traduzir_evento_webhook_valores_monetarios_sao_decimal_nao_float():
    cabecalho, _pagamentos = _traduzir_evento_webhook(_evento_base())

    assert isinstance(cabecalho["valor_constituido_total"], Decimal)
    assert isinstance(cabecalho["valor_constituido_antecipacao_pre"], Decimal)
    assert isinstance(cabecalho["valor_bloqueado"], Decimal)
    assert isinstance(cabecalho["valor_livre"], Decimal)
    assert isinstance(cabecalho["valor_total_ur"], Decimal)
    assert cabecalho["valor_constituido_total"] == Decimal("1000.33")


def test_traduzir_pagamento_webhook_valores_monetarios_sao_decimal_nao_float():
    pagamento = _traduzir_pagamento_webhook({
        "tipoInformacaoPagamento": 6,
        "valorAPagar": 250.75,
    })

    assert isinstance(pagamento["valor_a_pagar"], Decimal)
    assert pagamento["valor_a_pagar"] == Decimal("250.75")
    assert pagamento["valor_onerado"] is None
