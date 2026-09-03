from datetime import datetime, timezone

import pytest

from shared.cloudsql_client import get_db
from apps.agenda import repository
from apps.agenda.repository import (
    _CHAVE_UR,
    _com_filtros,
    precedencia_origem,
    upsert_agenda_ur,
)

FINANCIADOR_TESTE = "12345678000199"

CHAVE_TESTE = {
    "data_liquidacao": "2026-09-15",
    "entidade_registradora": "22246686000196",
    "cnpj_credenciadora": "01027058000191",
    "documento_ufr": "12345678000199",
    "documento_titular": "12345678000199",
    "codigo_arranjo": "VCC",
}


def _apagar_ur_teste():
    db = get_db(FINANCIADOR_TESTE)
    _com_filtros(db.table("agenda_ur_evento").delete(), CHAVE_TESTE, _CHAVE_UR).execute()
    _com_filtros(db.table("agenda_ur_pagamento").delete(), CHAVE_TESTE, _CHAVE_UR).execute()
    _com_filtros(db.table("agenda_ur").delete(), CHAVE_TESTE, _CHAVE_UR).execute()


@pytest.fixture(autouse=True)
def _limpar_ur_teste():
    _apagar_ur_teste()
    yield
    _apagar_ur_teste()


def _cabecalho(data_hora, origem, **overrides):
    base = {
        **CHAVE_TESTE,
        "constituicao": "1",
        "valor_constituido_total": 1000,
        "valor_constituido_antecipacao_pre": 0,
        "valor_bloqueado": 0,
        "valor_livre": 0,
        "valor_total_ur": 1000,
        "carteira": None,
        "data_hora_ultima_atualizacao": data_hora,
        "origem": origem,
        "origem_arquivo": None,
    }
    base.update(overrides)
    return base


def _pagamento(tipo, **overrides):
    base = {
        "tipo_informacao_pagamento": tipo,
        "indicador_efeitos_contrato": "",
        "identificador_cerc_contrato": None,
        "regras_divisao": None,
        "valor_onerado": None,
        "valor_constituido_efeito": None,
        "valor_a_pagar": 1000,
        "beneficiario": None,
        "data_liquidacao_efetiva": None,
        "valor_liquidacao_efetiva": None,
        "motivo_nao_pagamento": None,
        "domicilio": {},
    }
    base.update(overrides)
    return base


def test_precedencia_origem_ordem_esperada():
    assert precedencia_origem("WEBHOOK") > precedencia_origem("SINCRONO")
    assert precedencia_origem("SINCRONO") > precedencia_origem("ARQUIVO")


def test_upsert_cria_ur_pela_primeira_vez_e_gera_evento_captura():
    data_hora = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)

    resultado = upsert_agenda_ur(
        FINANCIADOR_TESTE,
        _cabecalho(data_hora, "SINCRONO", valor_total_ur=1500, valor_constituido_total=1500),
        pagamentos=[],
    )

    assert resultado["sobrescrito"] is True
    assert resultado["agenda_ur"]["valor_total_ur"] == 1500
    assert len(resultado["eventos"]) == 1
    assert resultado["eventos"][0]["tipo_evento"] == "CAPTURA"


def test_upsert_nao_sobrescreve_quando_lote_mais_antigo():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t2, "SINCRONO", valor_total_ur=1000), pagamentos=[])

    resultado = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "ARQUIVO", valor_total_ur=1), pagamentos=[])

    assert resultado["sobrescrito"] is False
    assert resultado["agenda_ur"]["valor_total_ur"] == 1000


def test_upsert_sobrescreve_quando_mais_recente():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "ARQUIVO", valor_total_ur=1000), pagamentos=[])

    resultado = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t2, "SINCRONO", valor_total_ur=2000), pagamentos=[])

    assert resultado["sobrescrito"] is True
    assert resultado["agenda_ur"]["valor_total_ur"] == 2000


def test_upsert_empate_de_timestamp_resolve_por_precedencia():
    t = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t, "ARQUIVO", valor_total_ur=1000), pagamentos=[])

    ganha = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t, "SINCRONO", valor_total_ur=2000), pagamentos=[])
    assert ganha["sobrescrito"] is True
    assert ganha["agenda_ur"]["valor_total_ur"] == 2000

    perde = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t, "ARQUIVO", valor_total_ur=3000), pagamentos=[])
    assert perde["sobrescrito"] is False
    assert perde["agenda_ur"]["valor_total_ur"] == 2000


def test_upsert_gera_evento_bloqueio_quando_valor_bloqueado_aumenta():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    upsert_agenda_ur(
        FINANCIADOR_TESTE,
        _cabecalho(t1, "SINCRONO", valor_bloqueado=0, valor_livre=1000),
        pagamentos=[],
    )

    resultado = upsert_agenda_ur(
        FINANCIADOR_TESTE,
        _cabecalho(t2, "WEBHOOK", valor_bloqueado=400, valor_livre=600),
        pagamentos=[],
    )

    tipos = {e["tipo_evento"] for e in resultado["eventos"]}
    assert tipos == {"BLOQUEIO"}


def test_upsert_gera_evento_disponibilizacao_quando_valor_livre_aumenta():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    upsert_agenda_ur(
        FINANCIADOR_TESTE,
        _cabecalho(t1, "SINCRONO", valor_bloqueado=400, valor_livre=0),
        pagamentos=[],
    )

    resultado = upsert_agenda_ur(
        FINANCIADOR_TESTE,
        _cabecalho(t2, "WEBHOOK", valor_bloqueado=0, valor_livre=400),
        pagamentos=[],
    )

    tipos = {e["tipo_evento"] for e in resultado["eventos"]}
    assert tipos == {"DISPONIBILIZACAO"}


def test_upsert_gera_evento_liquidacao_quando_pagamento_recebe_data_liquidacao_efetiva():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    pendente = _pagamento("5")
    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "SINCRONO"), pagamentos=[pendente])

    liquidado = _pagamento("5", data_liquidacao_efetiva="2026-09-15", valor_liquidacao_efetiva=1000)
    resultado = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t2, "WEBHOOK"), pagamentos=[liquidado])

    eventos_liquidacao = [e for e in resultado["eventos"] if e["tipo_evento"] == "LIQUIDACAO"]
    assert len(eventos_liquidacao) == 1
    assert eventos_liquidacao[0]["valor"] == 1000


def test_upsert_remove_pagamento_obsoleto_fora_do_lote_atual():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    p1 = _pagamento("1")
    p2 = _pagamento("2")
    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "SINCRONO"), pagamentos=[p1, p2])

    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t2, "WEBHOOK"), pagamentos=[p1])

    restantes = _com_filtros(
        get_db(FINANCIADOR_TESTE).table("agenda_ur_pagamento").select("tipo_informacao_pagamento"),
        CHAVE_TESTE, _CHAVE_UR,
    ).execute().data
    tipos_restantes = {r["tipo_informacao_pagamento"] for r in restantes}
    assert tipos_restantes == {"1"}


def test_upsert_pagamento_normaliza_indicador_none_para_vazio():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    pagamento = _pagamento("4", indicador_efeitos_contrato=None)

    resultado = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "SINCRONO"), pagamentos=[pagamento])

    assert resultado["pagamentos"][0]["indicador_efeitos_contrato"] == ""


def test_upsert_pagamento_com_indicador_nao_vazio_e_limpo_pela_chave_completa():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    com_efeito = _pagamento("4", indicador_efeitos_contrato="X")
    sem_efeito = _pagamento("4", indicador_efeitos_contrato="")
    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "SINCRONO"), pagamentos=[com_efeito, sem_efeito])

    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t2, "WEBHOOK"), pagamentos=[sem_efeito])

    restantes = _com_filtros(
        get_db(FINANCIADOR_TESTE).table("agenda_ur_pagamento").select("indicador_efeitos_contrato"),
        CHAVE_TESTE, _CHAVE_UR,
    ).execute().data
    assert {r["indicador_efeitos_contrato"] for r in restantes} == {""}


def test_upsert_evento_liquidacao_registra_discriminador_do_pagamento():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    pendente = _pagamento("5", indicador_efeitos_contrato="")
    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "SINCRONO"), pagamentos=[pendente])

    liquidado = _pagamento("5", indicador_efeitos_contrato="", data_liquidacao_efetiva="2026-09-15", valor_liquidacao_efetiva=1000)
    resultado = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t2, "WEBHOOK"), pagamentos=[liquidado])

    evento = next(e for e in resultado["eventos"] if e["tipo_evento"] == "LIQUIDACAO")
    assert evento["tipo_informacao_pagamento"] == "5"
    assert evento["indicador_efeitos_contrato"] == ""


def test_upsert_retorna_criado_true_na_primeira_vez_e_false_depois():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    primeiro = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "SINCRONO"), pagamentos=[])
    assert primeiro["criado"] is True

    segundo = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t2, "WEBHOOK"), pagamentos=[])
    assert segundo["criado"] is False


def test_upsert_retorna_pagamentos_vazio_quando_lote_e_descartado():
    t_recente = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    t_antigo = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t_recente, "SINCRONO"), pagamentos=[])

    resultado = upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t_antigo, "ARQUIVO"), pagamentos=[_pagamento("1")])

    assert resultado["sobrescrito"] is False
    assert resultado["pagamentos"] == []


def test_upsert_origem_invalida_falha_rapido():
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    with pytest.raises(KeyError):
        upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "sincrono"), pagamentos=[])


def test_upsert_e_atomico_nao_deixa_escrita_parcial_quando_falha_no_meio(monkeypatch):
    """Prova a correção de atomicidade: upsert_agenda_ur passou a rodar
    dentro de get_db(...).transaction() (shared/cloudsql_client.py), então
    uma falha após o cabeçalho já ter sido inserido (aqui, simulada no
    primeiro _registrar_evento — o evento CAPTURA da criação) precisa
    desfazer TAMBÉM o INSERT do cabeçalho, não só deixar de gravar o resto."""
    t1 = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)

    def _registrar_evento_com_falha(*args, **kwargs):
        raise RuntimeError("falha simulada no meio do upsert")

    monkeypatch.setattr(repository, "_registrar_evento", _registrar_evento_com_falha)

    with pytest.raises(RuntimeError, match="falha simulada"):
        upsert_agenda_ur(FINANCIADOR_TESTE, _cabecalho(t1, "SINCRONO"), pagamentos=[_pagamento("1")])

    ur_persistida = _com_filtros(
        get_db(FINANCIADOR_TESTE).table("agenda_ur").select("*"), CHAVE_TESTE, _CHAVE_UR,
    ).execute().data
    assert ur_persistida == []  # o INSERT do cabeçalho, que rodou ANTES da falha, também foi desfeito

    pagamentos_persistidos = _com_filtros(
        get_db(FINANCIADOR_TESTE).table("agenda_ur_pagamento").select("*"), CHAVE_TESTE, _CHAVE_UR,
    ).execute().data
    assert pagamentos_persistidos == []
