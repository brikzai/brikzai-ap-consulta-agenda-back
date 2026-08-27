"""Validações locais A01-A10 da consulta de agenda (SPEC03 §10.1, design
doc §7) — cada uma evita uma chamada desnecessária/inválida à CERC.

`codigo` em ValidacaoConsultaError é o código 105xxx da CERC quando existe
um equivalente direto no catálogo (SPEC03 §10); quando a regra é puramente
local, sem código CERC (A04, A06, A07, A08, A09, A10), é uma string
descritiva própria.

A07 (base autorizativa) valida só a FORMA do dict aqui — confirmar que o
opt-in está de fato ATIVO contra dado real do optin-service está bloqueado
pela lacuna do design doc §14 item 2 / §15 risco 4 (endpoint ainda não
existe lá). Não é uma lacuna nova deste módulo.
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from shared.cloudsql_client import get_db
from shared.tenant_config import get_tenant_config

_FUSO_CONSULTA = ZoneInfo("America/Sao_Paulo")
_TIPOS_AVALIACAO_AGENDA = {"avaliacao_agenda_basica_ap", "avaliacao_agenda_completa_ap"}
_LIMITE_CONSULTAS_ONLINE_POR_DIA = 10


class ValidacaoConsultaError(Exception):
    def __init__(self, codigo: str, mensagem: str):
        self.codigo = codigo
        self.mensagem = mensagem
        super().__init__(f"{codigo}: {mensagem}")


def _somente_digitos(documento: str) -> str:
    return "".join(c for c in documento if c.isdigit())


def _dv_cpf(digitos: str) -> str:
    def _calcular(base: str) -> str:
        pesos = list(range(len(base) + 1, 1, -1))
        soma = sum(int(d) * p for d, p in zip(base, pesos))
        resto = soma % 11
        return "0" if resto < 2 else str(11 - resto)

    dv1 = _calcular(digitos[:9])
    dv2 = _calcular(digitos[:9] + dv1)
    return dv1 + dv2


def _dv_cnpj(digitos: str) -> str:
    def _calcular(base: str) -> str:
        pesos = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2] if len(base) == 12 else [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
        soma = sum(int(d) * p for d, p in zip(base, pesos))
        resto = soma % 11
        return "0" if resto < 2 else str(11 - resto)

    dv1 = _calcular(digitos[:12])
    dv2 = _calcular(digitos[:12] + dv1)
    return dv1 + dv2


def validar_documento(documento: str, nome_campo: str, *, codigo_obrigatorio: str, codigo_invalido: str) -> None:
    """A01 — CPF (11) ou CNPJ (14) dígitos, zero-pad, com DV válido."""
    if not documento:
        raise ValidacaoConsultaError(codigo_obrigatorio, f"{nome_campo} é obrigatório")

    digitos = _somente_digitos(documento)
    if len(digitos) in (10, 13):
        digitos = digitos.zfill(11 if len(digitos) == 10 else 14)

    if len(digitos) == 11:
        base, dv = digitos[:9], digitos[9:]
        dv_esperado = _dv_cpf(base)
    elif len(digitos) == 14:
        base, dv = digitos[:12], digitos[12:]
        dv_esperado = _dv_cnpj(base)
    else:
        raise ValidacaoConsultaError(codigo_invalido, f"{nome_campo} deve ter 11 (CPF) ou 14 (CNPJ) dígitos")

    if dv != dv_esperado:
        raise ValidacaoConsultaError(codigo_invalido, f"{nome_campo} tem dígito verificador inválido")


def validar_janela_datas(data_inicio: date, data_fim: date) -> None:
    """A02 — dataFim >= dataInicio (105016)."""
    if data_fim < data_inicio:
        raise ValidacaoConsultaError("105016", "dataFim deve ser maior ou igual a dataInicio")


def validar_lista_nao_vazia(lista, nome_campo: str, codigo: str) -> None:
    """A03 — listas não vazias (105004 credenciadoras, 105008 arranjos)."""
    if not lista:
        raise ValidacaoConsultaError(codigo, f"{nome_campo} não pode ser vazia")


def validar_sem_mistura_curinga(lista, nome_campo: str) -> None:
    """A04 — não misturar '99T' com valores específicos na mesma lista."""
    if "99T" in lista and len(lista) > 1:
        raise ValidacaoConsultaError("MISTURA_CURINGA_INVALIDA", f"{nome_campo} não pode misturar '99T' com valores específicos")


def validar_arranjos_no_dominio(financiador_id: str, arranjos: list) -> None:
    """A05 — arranjos no domínio vigente (105009). '99T' nunca é consultado."""
    especificos = [a for a in arranjos if a != "99T"]
    if not especificos:
        return

    db = get_db(financiador_id)
    ativos = db.table("dominio_arranjo").select("codigo").eq("ativo", True).execute().data
    codigos_ativos = {row["codigo"] for row in ativos}

    invalidos = [a for a in especificos if a not in codigos_ativos]
    if invalidos:
        raise ValidacaoConsultaError("105009", f"arranjo(s) fora do domínio vigente: {', '.join(invalidos)}")


def validar_tipo_avaliacao(tipo_avaliacao) -> None:
    """A06 — tipoAvaliacao restrito aos valores de agenda (avaliacao_contrato_* pertence à SPEC 02)."""
    if tipo_avaliacao is not None and tipo_avaliacao not in _TIPOS_AVALIACAO_AGENDA:
        raise ValidacaoConsultaError("TIPO_AVALIACAO_INVALIDO", f"tipoAvaliacao inválido para agenda: {tipo_avaliacao!r}")


def validar_base_autorizativa(base_autorizativa: dict) -> None:
    """A07 — valida só a forma (tipo/id presentes e coerentes). Ver nota de módulo."""
    tipo = base_autorizativa.get("tipo")
    id_ = base_autorizativa.get("id")
    if tipo not in ("OPTIN", "CONTRATO"):
        raise ValidacaoConsultaError("SEM_BASE_AUTORIZATIVA", "baseAutorizativa.tipo deve ser OPTIN ou CONTRATO")
    if not id_:
        raise ValidacaoConsultaError("SEM_BASE_AUTORIZATIVA", "baseAutorizativa.id é obrigatório")


def validar_rate_limit_online(financiador_id: str, documento_ufr: str, modo: str) -> None:
    """A08 — rate limit de consulta online por UFR (default 10/dia, fuso America/Sao_Paulo)."""
    if modo != "ONLINE":
        return

    db = get_db(financiador_id)
    agora_sp = datetime.now(_FUSO_CONSULTA)
    inicio_dia = datetime.combine(agora_sp.date(), datetime.min.time(), tzinfo=_FUSO_CONSULTA)

    resultado = (
        db.table("consulta_agenda")
        .select("id", count="exact")
        .eq("filtro_ufr", documento_ufr)
        .eq("modo", "ONLINE")
        .gte("iniciada_em", inicio_dia)
        .execute()
    )
    if resultado.count is not None and resultado.count >= _LIMITE_CONSULTAS_ONLINE_POR_DIA:
        raise ValidacaoConsultaError(
            "RATE_LIMIT_EXCEDIDO",
            f"limite de {_LIMITE_CONSULTAS_ONLINE_POR_DIA} consultas online/dia atingido para {documento_ufr}",
        )


def validar_carteira_presente(financiador_id: str, carteira) -> None:
    """A09 — carteira obrigatória quando o participante (financiador) é 'Prestador de Serviço'."""
    config = get_tenant_config(financiador_id)
    if config.get("participante_tipo") == "PRESTADOR_SERVICO" and not carteira:
        raise ValidacaoConsultaError("CARTEIRA_OBRIGATORIA", "carteira é obrigatória para participante do tipo Prestador de Serviço")


def validar_politica_consulta(financiador_id: str, motivo: str, modo: str) -> None:
    """A10 — fail-closed: sem política ativa para o motivo, barra sem chamar a CERC (design doc §7)."""
    db = get_db(financiador_id)
    politica = db.table("politica_consulta").select("*").eq("motivo", motivo).eq("ativo", True).execute().data
    if not politica:
        raise ValidacaoConsultaError("POLITICA_NAO_CONFIGURADA", f"nenhuma política ativa para o motivo {motivo!r}")

    modos_permitidos = politica[0]["modos_permitidos"]
    if modo not in modos_permitidos:
        raise ValidacaoConsultaError(
            "MODO_NAO_PERMITIDO",
            f"modo {modo!r} não permitido para o motivo {motivo!r} (permitidos: {modos_permitidos})",
        )


_MODOS_VALIDOS = {"BATCH", "ONLINE"}


def validar_modos_permitidos(modos_permitidos) -> None:
    """Valida modos_permitidos <@ ARRAY['BATCH','ONLINE'] (design doc §7)
    — checagem de aplicação, não CHECK de banco (mesmo estilo dos irmãos:
    domínio muda por decisão de produto, não regra imutável do banco)."""
    if not modos_permitidos:
        raise ValidacaoConsultaError("MODOS_PERMITIDOS_VAZIO", "modosPermitidos não pode ser vazio")
    invalidos = [m for m in modos_permitidos if m not in _MODOS_VALIDOS]
    if invalidos:
        raise ValidacaoConsultaError(
            "MODOS_PERMITIDOS_INVALIDO", f"modosPermitidos contém valor(es) inválido(s): {', '.join(invalidos)}",
        )


def validar_consulta(financiador_id: str, consulta: dict) -> None:
    """Roda A01-A10 na ordem do design doc, antes de qualquer chamada à CERC."""
    validar_documento(
        consulta["documento_ufr"], "documentoUsuarioFinalRecebedor",
        codigo_obrigatorio="105006", codigo_invalido="105007",
    )
    if consulta.get("documento_titular"):
        validar_documento(
            consulta["documento_titular"], "documentoTitular",
            codigo_obrigatorio="105014", codigo_invalido="105015",
        )
    validar_janela_datas(consulta["data_inicio"], consulta["data_fim"])
    validar_lista_nao_vazia(consulta["credenciadoras"], "listaCnpjCredenciadora", "105004")
    validar_lista_nao_vazia(consulta["arranjos"], "listaCodigoArranjoPagamento", "105008")
    validar_sem_mistura_curinga(consulta["credenciadoras"], "listaCnpjCredenciadora")
    validar_sem_mistura_curinga(consulta["arranjos"], "listaCodigoArranjoPagamento")
    validar_arranjos_no_dominio(financiador_id, consulta["arranjos"])
    validar_tipo_avaliacao(consulta.get("tipo_avaliacao"))
    validar_base_autorizativa(consulta["base_autorizativa"])
    validar_rate_limit_online(financiador_id, consulta["documento_ufr"], consulta["modo"])
    validar_carteira_presente(financiador_id, consulta.get("carteira"))
    validar_politica_consulta(financiador_id, consulta["motivo"], consulta["modo"])
