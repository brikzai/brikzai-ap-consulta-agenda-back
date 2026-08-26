"""Parser do arquivo CERC-AP005/AP005A/AP005B (design doc §9, SPEC03 §6).

Um único parser atende aos três leiautes (§6.5) — a origem vem do nome do
arquivo, o conteúdo é idêntico. Cada linha do CSV representa NO MÁXIMO um
efeito de pagamento de uma UR (decisão do Plano 08, Global Constraints: o
arquivo real da CERC repete as colunas 1-11/13-16 da UR a cada linha e
varia só o bloco 12.x — mesma modelagem que a API síncrona/webhook usam
para lista de pagamentos, só que como linhas de arquivo em vez de itens de
lista JSON; ainda não confirmado com a CERC, SPEC03 §13). O ingestor
(apps/agenda/importar_ap005.py) é quem agrupa linhas consecutivas da mesma
UR num único upsert_agenda_ur.

Detecção de leiaute por contagem de colunas FÍSICAS do CSV — não a
numeração de campos do §6.2, que trata "coluna 12" como um bloco nomeado
de 16 subcampos (aqui cada subcampo é uma célula própria):
- <= 4 colunas: leiaute reduzido "sem agenda" (§6.2) — resultado válido,
  nunca rejeitado.
- 15 colunas: leiaute completo sem bloco de pagamento (UR "baixada" sem
  pagamentos a fazer, nota da coluna 12 em §6.2).
- 30 colunas: leiaute completo com pagamento, sem a coluna 12.16 (arquivo
  anterior a 03/11/2025 ou ambiente antigo, §6.4).
- 31 colunas: leiaute completo com pagamento e coluna 12.16.
Qualquer outra contagem é uma linha inválida.
"""
import re
from datetime import date, datetime

_NOME_ARQUIVO_RE = re.compile(r"^(CERC-AP005[AB]?)_(\d{8})_(\d{8})_(\d{7})_ret\.csv(\.gz)?$")
_TIPOS_INFORMACAO_PAGAMENTO_VALIDOS = {"1", "2", "3", "4", "5", "6", "7", "8"}

_N_REDUZIDO_MAX = 4
_N_SEM_PAGAMENTO = 15
_N_COM_PAGAMENTO_SEM_1216 = 30
_N_COM_PAGAMENTO_COM_1216 = 31


class NomeArquivoInvalidoError(Exception):
    pass


class LinhaInvalidaError(Exception):
    pass


def parse_nome_arquivo(nome_arquivo: str) -> dict:
    """Extrai (tipo_leiaute, ident_ic, data_req, seq) do nome do arquivo
    (SPEC03 §6.1: `{Tipo_Leiaute}_{Ident_IC}_{DataReq}_{Seq}_ret.csv`).
    `tipo_leiaute` mantém o prefixo `CERC-` — decisão do Plano 08 (design
    doc §15 risco 5): agenda_ur.origem_arquivo grava o MESMO valor, sem
    conversão, fechando o descompasso de vocabulário registrado ali."""
    match = _NOME_ARQUIVO_RE.match(nome_arquivo)
    if not match:
        raise NomeArquivoInvalidoError(
            f"nome de arquivo fora do padrão CERC-AP005[A|B]_{{ident_ic}}_{{data_req}}_{{seq}}_ret.csv[.gz]: {nome_arquivo!r}"
        )
    tipo_leiaute, ident_ic, data_req_str, seq_str, gz = match.groups()
    return {
        "tipo_leiaute": tipo_leiaute,
        "ident_ic": ident_ic,
        "data_req": datetime.strptime(data_req_str, "%Y%m%d").date(),
        "seq": int(seq_str),
        "comprimido": gz is not None,
    }


def _val(campos: list, indice: int) -> str:
    return campos[indice].strip() if indice < len(campos) else ""


def _campo(campos: list, indice: int, nome: str, *, obrigatorio: bool = True):
    valor = _val(campos, indice)
    if obrigatorio and not valor:
        raise LinhaInvalidaError(f"{nome} é obrigatório e veio vazio")
    return valor or None


def _parse_decimal(campos: list, indice: int, nome: str, *, obrigatorio: bool, default=0):
    valor = _val(campos, indice)
    if not valor:
        if obrigatorio:
            raise LinhaInvalidaError(f"{nome} é obrigatório e veio vazio")
        return default
    try:
        return float(valor)
    except ValueError:
        raise LinhaInvalidaError(f"{nome} não é um decimal válido: {valor!r}") from None


def _parse_data(valor: str, nome: str) -> date:
    try:
        return date.fromisoformat(valor)
    except ValueError:
        raise LinhaInvalidaError(f"{nome} não é uma data válida (AAAA-MM-DD): {valor!r}") from None


def _parse_data_hora(valor: str, nome: str) -> datetime:
    try:
        return datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        raise LinhaInvalidaError(f"{nome} não é um RFC3339 válido: {valor!r}") from None


def detectar_layout(n_campos: int) -> str:
    if n_campos <= _N_REDUZIDO_MAX:
        return "REDUZIDO"
    if n_campos == _N_SEM_PAGAMENTO:
        return "SEM_PAGAMENTO"
    if n_campos == _N_COM_PAGAMENTO_SEM_1216:
        return "COM_PAGAMENTO_SEM_1216"
    if n_campos == _N_COM_PAGAMENTO_COM_1216:
        return "COM_PAGAMENTO_COM_1216"
    raise LinhaInvalidaError(f"contagem de colunas inesperada: {n_campos}")


def _traduzir_pagamento(sub: list, tem_1216: bool) -> dict:
    """`sub` = campos 12.1..12.15 (15 itens) ou 12.1..12.16 (16 itens),
    já fatiados pelo chamador. Índices 0-based conforme SPEC03 §6.2."""
    tipo_informacao_pagamento = _campo(sub, 12, "tipoInformacaoPagamento (12.13)")
    if tipo_informacao_pagamento not in _TIPOS_INFORMACAO_PAGAMENTO_VALIDOS:
        raise LinhaInvalidaError(f"tipoInformacaoPagamento (12.13) fora do domínio 1-8: {tipo_informacao_pagamento!r}")

    data_liq_efetiva_bruta = _campo(sub, 8, "data de liquidação efetiva (12.9)", obrigatorio=False)

    return {
        "tipo_informacao_pagamento": tipo_informacao_pagamento,
        # Risco 6 do design doc: NOT NULL DEFAULT '' no schema — "or ''", nunca "or None".
        "indicador_efeitos_contrato": _campo(sub, 13, "indicador de ordem do efeito (12.14)", obrigatorio=False) or "",
        "identificador_cerc_contrato": _campo(sub, 15, "identificador CERC do contrato (12.16)", obrigatorio=False) if tem_1216 else None,
        "regras_divisao": _campo(sub, 10, "regra de divisão (12.11)", obrigatorio=False),
        "valor_onerado": _parse_decimal(sub, 11, "valor onerado na UR (12.12)", obrigatorio=False, default=None),
        "valor_constituido_efeito": _parse_decimal(sub, 14, "valor constituído do efeito (12.15)", obrigatorio=False, default=None),
        "valor_a_pagar": _parse_decimal(sub, 6, "valor a pagar (12.7)", obrigatorio=True),
        "beneficiario": _campo(sub, 7, "beneficiário (12.8)", obrigatorio=False),
        "data_liquidacao_efetiva": _parse_data(data_liq_efetiva_bruta, "data de liquidação efetiva (12.9)") if data_liq_efetiva_bruta else None,
        "valor_liquidacao_efetiva": _parse_decimal(sub, 9, "valor de liquidação efetiva (12.10)", obrigatorio=False, default=None),
        "domicilio": {
            "numeroDocumentoTitular": _campo(sub, 0, "número documento titular (12.1)"),
            "tipoConta": _campo(sub, 1, "tipo de conta (12.2)"),
            "compe": _campo(sub, 2, "COMPE (12.3)", obrigatorio=False),
            "ispb": _campo(sub, 3, "ISPB (12.4)"),
            "agencia": _campo(sub, 4, "agência (12.5)", obrigatorio=False),
            "numeroConta": _campo(sub, 5, "número da conta (12.6)"),
        },
    }


def traduzir_linha(campos: list, tipo_leiaute: str):
    """Traduz uma linha do CSV para (cabecalho, pagamento_ou_none), ou
    None se a linha for do leiaute reduzido "sem agenda" (§6.2) — nesse
    caso não há UR nenhuma para gravar, e a linha NUNCA é rejeitada.
    Levanta LinhaInvalidaError com o motivo para qualquer linha malformada
    — o chamador (importar_ap005.py) grava em agenda_ur_rejeitada e segue
    para a próxima linha (SPEC03 §6.6: "nunca abortar o arquivo inteiro")."""
    layout = detectar_layout(len(campos))
    if layout == "REDUZIDO":
        return None

    cabecalho = {
        "entidade_registradora": _campo(campos, 1, "entidade registradora (col. 2)"),
        "cnpj_credenciadora": _campo(campos, 2, "instituição credenciadora (col. 3)"),
        "documento_ufr": _campo(campos, 3, "usuário final recebedor (col. 4)"),
        "codigo_arranjo": _campo(campos, 4, "arranjo de pagamento (col. 5)"),
        "data_liquidacao": _parse_data(_campo(campos, 5, "data de liquidação (col. 6)"), "data de liquidação (col. 6)"),
        "documento_titular": _campo(campos, 6, "titular da UR (col. 7)"),
        "constituicao": _campo(campos, 7, "constituição da UR (col. 8)"),
        "valor_constituido_total": _parse_decimal(campos, 8, "valor constituído total (col. 9)", obrigatorio=True),
        "valor_constituido_antecipacao_pre": _parse_decimal(campos, 9, "valor constituído antecipação pré (col. 10)", obrigatorio=False, default=0),
        "valor_bloqueado": _parse_decimal(campos, 10, "valor bloqueado (col. 11)", obrigatorio=False, default=0),
        "origem": "ARQUIVO",
        "origem_arquivo": tipo_leiaute,
    }

    if layout == "SEM_PAGAMENTO":
        cauda = campos[11:]
        pagamento = None
    else:
        tem_1216 = layout == "COM_PAGAMENTO_COM_1216"
        tamanho_bloco = 16 if tem_1216 else 15
        bloco_12 = campos[11:11 + tamanho_bloco]
        cauda = campos[11 + tamanho_bloco:]
        pagamento = _traduzir_pagamento(bloco_12, tem_1216)

    cabecalho["carteira"] = _campo(cauda, 0, "carteira (col. 13)", obrigatorio=False)
    cabecalho["valor_livre"] = _parse_decimal(cauda, 1, "valor livre (col. 14)", obrigatorio=False, default=0)
    cabecalho["valor_total_ur"] = _parse_decimal(cauda, 2, "valor total da UR (col. 15)", obrigatorio=True)
    cabecalho["data_hora_ultima_atualizacao"] = _parse_data_hora(
        _campo(cauda, 3, "data/hora última atualização (col. 16)"), "data/hora última atualização (col. 16)",
    )
    return cabecalho, pagamento
