import json
from datetime import date, datetime, timezone

import pytest

from shared.cloudsql_client import get_db
import shared.tenant_config as tenant_config_module
from apps.agenda.validation import (
    ValidacaoConsultaError,
    validar_arranjos_no_dominio,
    validar_base_autorizativa,
    validar_carteira_presente,
    validar_consulta,
    validar_documento,
    validar_janela_datas,
    validar_lista_nao_vazia,
    validar_politica_consulta,
    validar_rate_limit_online,
    validar_sem_mistura_curinga,
    validar_tipo_avaliacao,
)

FINANCIADOR_TESTE = "12345678000199"
CNPJ_VALIDO = "11222333000181"
CPF_VALIDO = "11144477735"
CNPJ_SEM_ZERO_A_ESQUERDA = "1222333000128"   # zero-padded == 01222333000128, DV válido
CPF_SEM_ZERO_A_ESQUERDA = "1114447722"        # zero-padded == 01114447722, DV válido
CNPJ_DV_INVALIDO = "12345678000199"           # DV real seria ...95, não ...99


def test_validar_documento_aceita_cnpj_valido():
    validar_documento(CNPJ_VALIDO, "documentoUsuarioFinalRecebedor", codigo_obrigatorio="105006", codigo_invalido="105007")


def test_validar_documento_aceita_cpf_valido():
    validar_documento(CPF_VALIDO, "documentoTitular", codigo_obrigatorio="105014", codigo_invalido="105015")


def test_validar_documento_zero_pad_cnpj_sem_zero_a_esquerda():
    validar_documento(CNPJ_SEM_ZERO_A_ESQUERDA, "documentoUsuarioFinalRecebedor", codigo_obrigatorio="105006", codigo_invalido="105007")


def test_validar_documento_zero_pad_cpf_sem_zero_a_esquerda():
    validar_documento(CPF_SEM_ZERO_A_ESQUERDA, "documentoTitular", codigo_obrigatorio="105014", codigo_invalido="105015")


def test_validar_documento_rejeita_dv_invalido():
    with pytest.raises(ValidacaoConsultaError) as exc_info:
        validar_documento(CNPJ_DV_INVALIDO, "documentoUsuarioFinalRecebedor", codigo_obrigatorio="105006", codigo_invalido="105007")
    assert exc_info.value.codigo == "105007"


def test_validar_documento_rejeita_vazio():
    with pytest.raises(ValidacaoConsultaError) as exc_info:
        validar_documento("", "documentoUsuarioFinalRecebedor", codigo_obrigatorio="105006", codigo_invalido="105007")
    assert exc_info.value.codigo == "105006"


def test_validar_janela_datas_aceita_data_fim_igual_inicio():
    validar_janela_datas(date(2026, 9, 1), date(2026, 9, 1))


def test_validar_janela_datas_rejeita_data_fim_menor():
    with pytest.raises(ValidacaoConsultaError) as exc_info:
        validar_janela_datas(date(2026, 9, 10), date(2026, 9, 1))
    assert exc_info.value.codigo == "105016"


def test_validar_lista_nao_vazia_rejeita_lista_vazia():
    with pytest.raises(ValidacaoConsultaError) as exc_info:
        validar_lista_nao_vazia([], "listaCnpjCredenciadora", "105004")
    assert exc_info.value.codigo == "105004"


def test_validar_lista_nao_vazia_aceita_lista_preenchida():
    validar_lista_nao_vazia(["99T"], "listaCnpjCredenciadora", "105004")


def test_validar_sem_mistura_curinga_rejeita_99t_com_especifico():
    with pytest.raises(ValidacaoConsultaError):
        validar_sem_mistura_curinga(["99T", "VCC"], "listaCodigoArranjoPagamento")


def test_validar_sem_mistura_curinga_aceita_so_curinga():
    validar_sem_mistura_curinga(["99T"], "listaCodigoArranjoPagamento")


def test_validar_sem_mistura_curinga_aceita_so_especificos():
    validar_sem_mistura_curinga(["VCC", "VCD"], "listaCodigoArranjoPagamento")


def test_validar_arranjos_no_dominio_aceita_curinga_sem_consultar_banco():
    validar_arranjos_no_dominio(FINANCIADOR_TESTE, ["99T"])


def test_validar_arranjos_no_dominio_aceita_codigo_ativo():
    db = get_db(FINANCIADOR_TESTE)
    db.table("dominio_arranjo").delete().eq("codigo", "VCC-TESTE").execute()
    db.table("dominio_arranjo").insert({
        "codigo": "VCC-TESTE", "descricao": "Visa Crédito", "ativo": True,
        "atualizado_em": datetime.now(timezone.utc),
    }).execute()
    try:
        validar_arranjos_no_dominio(FINANCIADOR_TESTE, ["VCC-TESTE"])
    finally:
        db.table("dominio_arranjo").delete().eq("codigo", "VCC-TESTE").execute()


def test_validar_arranjos_no_dominio_rejeita_codigo_desconhecido():
    with pytest.raises(ValidacaoConsultaError) as exc_info:
        validar_arranjos_no_dominio(FINANCIADOR_TESTE, ["CODIGO-INEXISTENTE-XYZ"])
    assert exc_info.value.codigo == "105009"


def test_validar_tipo_avaliacao_aceita_none():
    validar_tipo_avaliacao(None)


def test_validar_tipo_avaliacao_aceita_valor_de_agenda():
    validar_tipo_avaliacao("avaliacao_agenda_basica_ap")


def test_validar_tipo_avaliacao_rejeita_valor_de_contrato():
    with pytest.raises(ValidacaoConsultaError):
        validar_tipo_avaliacao("avaliacao_contrato_completa")


def test_validar_base_autorizativa_aceita_forma_valida():
    validar_base_autorizativa({"tipo": "OPTIN", "id": "opt_123"})


def test_validar_base_autorizativa_rejeita_tipo_invalido():
    with pytest.raises(ValidacaoConsultaError):
        validar_base_autorizativa({"tipo": "OUTRO", "id": "x"})


def test_validar_base_autorizativa_rejeita_id_ausente():
    with pytest.raises(ValidacaoConsultaError):
        validar_base_autorizativa({"tipo": "OPTIN", "id": ""})


def test_validar_rate_limit_online_ignora_modo_batch():
    validar_rate_limit_online(FINANCIADOR_TESTE, CNPJ_VALIDO, "BATCH")


def test_validar_rate_limit_online_bloqueia_apos_limite():
    db = get_db(FINANCIADOR_TESTE)
    ufr = "22222222000122"
    db.table("consulta_agenda").delete().eq("filtro_ufr", ufr).execute()
    agora = datetime.now(timezone.utc)
    try:
        for i in range(10):
            db.table("consulta_agenda").insert({
                "id": f"rl-teste-{i}",
                "modo": "ONLINE",
                "status": "COMPLETA",
                "filtro_ufr": ufr,
                "filtro_credenciadoras": ["99T"],
                "filtro_arranjos": ["99T"],
                "filtro_data_inicio": date(2026, 9, 1),
                "filtro_data_fim": date(2026, 9, 30),
                "base_autorizativa_tipo": "OPTIN",
                "base_autorizativa_id": "opt_1",
                "motivo": "TESTE",
                "ator": "teste@teste.com",
                "iniciada_em": agora,
            }).execute()

        with pytest.raises(ValidacaoConsultaError) as exc_info:
            validar_rate_limit_online(FINANCIADOR_TESTE, ufr, "ONLINE")
        assert exc_info.value.codigo == "RATE_LIMIT_EXCEDIDO"
    finally:
        db.table("consulta_agenda").delete().eq("filtro_ufr", ufr).execute()


def test_validar_carteira_presente_exige_carteira_para_prestador_servico(monkeypatch):
    monkeypatch.setenv(f"TENANT_{FINANCIADOR_TESTE}_CONFIG", json.dumps({"participante_tipo": "PRESTADOR_SERVICO"}))
    tenant_config_module._cache.clear()
    try:
        with pytest.raises(ValidacaoConsultaError):
            validar_carteira_presente(FINANCIADOR_TESTE, None)
        validar_carteira_presente(FINANCIADOR_TESTE, "CARTEIRA-01")
    finally:
        tenant_config_module._cache.clear()


def test_validar_carteira_presente_dispensa_para_outros_participantes(monkeypatch):
    monkeypatch.setenv(f"TENANT_{FINANCIADOR_TESTE}_CONFIG", json.dumps({}))
    tenant_config_module._cache.clear()
    try:
        validar_carteira_presente(FINANCIADOR_TESTE, None)
    finally:
        tenant_config_module._cache.clear()


def test_validar_politica_consulta_fail_closed_sem_politica():
    db = get_db(FINANCIADOR_TESTE)
    db.table("politica_consulta").delete().eq("motivo", "MOTIVO-INEXISTENTE").execute()
    with pytest.raises(ValidacaoConsultaError) as exc_info:
        validar_politica_consulta(FINANCIADOR_TESTE, "MOTIVO-INEXISTENTE", "BATCH")
    assert exc_info.value.codigo == "POLITICA_NAO_CONFIGURADA"


def test_validar_politica_consulta_rejeita_modo_nao_permitido():
    db = get_db(FINANCIADOR_TESTE)
    db.table("politica_consulta").delete().eq("motivo", "TESTE-MODO").execute()
    db.table("politica_consulta").insert({
        "id": "pol-teste-modo", "motivo": "TESTE-MODO", "modos_permitidos": ["BATCH"], "ativo": True,
    }).execute()
    try:
        with pytest.raises(ValidacaoConsultaError) as exc_info:
            validar_politica_consulta(FINANCIADOR_TESTE, "TESTE-MODO", "ONLINE")
        assert exc_info.value.codigo == "MODO_NAO_PERMITIDO"
        validar_politica_consulta(FINANCIADOR_TESTE, "TESTE-MODO", "BATCH")
    finally:
        db.table("politica_consulta").delete().eq("motivo", "TESTE-MODO").execute()


def _consulta_base(**overrides):
    base = {
        "modo": "BATCH",
        "documento_ufr": CNPJ_VALIDO,
        "documento_titular": None,
        "credenciadoras": ["99T"],
        "arranjos": ["99T"],
        "data_inicio": date(2026, 9, 1),
        "data_fim": date(2026, 9, 30),
        "tipo_avaliacao": None,
        "participante": None,
        "carteira": None,
        "base_autorizativa": {"tipo": "OPTIN", "id": "opt_1"},
        "motivo": "TESTE-VALIDAR-CONSULTA",
        "ator": "teste@teste.com",
        "origem_ip": None,
    }
    base.update(overrides)
    return base


def test_validar_consulta_passa_com_politica_ativa_e_dados_validos():
    db = get_db(FINANCIADOR_TESTE)
    db.table("politica_consulta").delete().eq("motivo", "TESTE-VALIDAR-CONSULTA").execute()
    db.table("politica_consulta").insert({
        "id": "pol-teste-validar-consulta", "motivo": "TESTE-VALIDAR-CONSULTA",
        "modos_permitidos": ["BATCH", "ONLINE"], "ativo": True,
    }).execute()
    try:
        validar_consulta(FINANCIADOR_TESTE, _consulta_base())
    finally:
        db.table("politica_consulta").delete().eq("motivo", "TESTE-VALIDAR-CONSULTA").execute()


def test_validar_consulta_falha_closed_sem_politica_configurada():
    db = get_db(FINANCIADOR_TESTE)
    db.table("politica_consulta").delete().eq("motivo", "TESTE-SEM-POLITICA").execute()
    with pytest.raises(ValidacaoConsultaError) as exc_info:
        validar_consulta(FINANCIADOR_TESTE, _consulta_base(motivo="TESTE-SEM-POLITICA"))
    assert exc_info.value.codigo == "POLITICA_NAO_CONFIGURADA"
