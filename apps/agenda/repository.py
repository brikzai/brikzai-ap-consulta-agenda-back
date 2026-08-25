"""Repositório de upsert de agenda_ur — regra de frescor e trilha de eventos
(design doc §8, §15 riscos 1, 7 e 12).

upsert_agenda_ur(financiador_id, cabecalho, pagamentos) é o único ponto de
escrita de uma UR, usado pelos Planos 06 (CERC síncrono), 07 (webhook) e 08
(arquivo AP005) — cada um traduz sua origem (SINCRONO/WEBHOOK/ARQUIVO) para
este mesmo formato e chama esta função, que decide se sobrescreve
(precedência WEBHOOK > SINCRONO > ARQUIVO, "mais recente sempre vence" —
§8), atualiza agenda_ur_pagamento removendo linhas de efeitos que não
vieram neste lote (§15 risco 7), e grava em agenda_ur_evento as transições
Captura/Bloqueio/Disponibilização/Liquidação que a timeline do front
("Eventos de Mutação" em ScheduleModal.tsx) precisa exibir (§15 risco 12).

`pagamentos` deve ser a lista COMPLETA e atual de efeitos da UR neste lote,
nunca um delta — essa premissa é o que permite decidir quais linhas antigas
ficaram obsoletas. `cabecalho["data_hora_ultima_atualizacao"]` deve ser um
datetime.datetime com tzinfo (RFC3339 já parseado pelo chamador).
"""
from datetime import datetime
from typing import Optional

from ulid import ULID

from shared.cloudsql_client import get_db

_CHAVE_UR = (
    "data_liquidacao",
    "entidade_registradora",
    "cnpj_credenciadora",
    "documento_ufr",
    "documento_titular",
    "codigo_arranjo",
)

_CHAVE_PAGAMENTO_EXTRA = ("tipo_informacao_pagamento", "indicador_efeitos_contrato")

_PRECEDENCIA_ORIGEM = {"WEBHOOK": 3, "SINCRONO": 2, "ARQUIVO": 1}


def precedencia_origem(origem: str) -> int:
    return _PRECEDENCIA_ORIGEM[origem]


def _com_filtros(query, valores: dict, campos):
    for campo in campos:
        query = query.eq(campo, valores[campo])
    return query


def _buscar_um(db, tabela: str, valores: dict, campos) -> Optional[dict]:
    resultado = _com_filtros(db.table(tabela).select("*"), valores, campos).execute()
    return resultado.data[0] if resultado.data else None


def _como_datetime(valor) -> datetime:
    # O driver (pg8000, via SQLAlchemy text()) pode devolver um
    # datetime.datetime nativo ou (dependendo da coluna/versão) uma string
    # RFC3339 — normaliza os dois casos antes de comparar, já que isso
    # nunca foi exercitado num teste desta base ainda.
    if isinstance(valor, datetime):
        return valor
    return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))


def _deve_sobrescrever(existente: Optional[dict], nova_data_hora: datetime, nova_origem: str) -> bool:
    if existente is None:
        return True
    atual = _como_datetime(existente["data_hora_ultima_atualizacao"])
    nova = _como_datetime(nova_data_hora)
    if nova > atual:
        return True
    if nova == atual:
        return precedencia_origem(nova_origem) > precedencia_origem(existente["origem"])
    return False


def _registrar_evento(
    db, chave: dict, tipo_evento: str, origem: str, valor, ocorrido_em: datetime,
    *, tipo_informacao_pagamento=None, indicador_efeitos_contrato: str = "",
) -> dict:
    evento = {campo: chave[campo] for campo in _CHAVE_UR}
    evento.update({
        "id": str(ULID()),
        "tipo_evento": tipo_evento,
        "origem": origem,
        "valor": valor,
        "ocorrido_em": ocorrido_em,
        "tipo_informacao_pagamento": tipo_informacao_pagamento,
        "indicador_efeitos_contrato": indicador_efeitos_contrato,
    })
    return db.table("agenda_ur_evento").insert(evento).execute().data[0]


def _upsert_cabecalho(db, chave: dict, cabecalho: dict, existente: Optional[dict], ocorrido_em: datetime) -> dict:
    dados = {**cabecalho, "atualizado_em": ocorrido_em}
    if existente is None:
        return db.table("agenda_ur").insert(dados).execute().data[0]
    return _com_filtros(db.table("agenda_ur").update(dados), chave, _CHAVE_UR).execute().data[0]


def _eventos_do_cabecalho(db, chave: dict, origem: str, existente: Optional[dict], novo: dict, ocorrido_em: datetime) -> list:
    if existente is None:
        return [_registrar_evento(db, chave, "CAPTURA", origem, novo["valor_total_ur"], ocorrido_em)]

    eventos = []
    if novo["valor_bloqueado"] > existente["valor_bloqueado"]:
        eventos.append(_registrar_evento(db, chave, "BLOQUEIO", origem, novo["valor_bloqueado"], ocorrido_em))
    if novo["valor_livre"] > existente["valor_livre"]:
        eventos.append(_registrar_evento(db, chave, "DISPONIBILIZACAO", origem, novo["valor_livre"], ocorrido_em))
    return eventos


def _upsert_pagamento(db, chave: dict, pagamento: dict, ocorrido_em: datetime):
    chave_pagamento = dict(chave)
    chave_pagamento["tipo_informacao_pagamento"] = pagamento["tipo_informacao_pagamento"]
    chave_pagamento["indicador_efeitos_contrato"] = pagamento.get("indicador_efeitos_contrato") or ""

    campos_chave = _CHAVE_UR + _CHAVE_PAGAMENTO_EXTRA
    existente = _buscar_um(db, "agenda_ur_pagamento", chave_pagamento, campos_chave)

    dados = {
        **chave_pagamento,
        **pagamento,
        "atualizado_em": ocorrido_em,
        "indicador_efeitos_contrato": chave_pagamento["indicador_efeitos_contrato"],
    }
    if existente is None:
        gravado = db.table("agenda_ur_pagamento").insert(dados).execute().data[0]
    else:
        gravado = _com_filtros(
            db.table("agenda_ur_pagamento").update(dados), chave_pagamento, campos_chave
        ).execute().data[0]

    liquidou_agora = (
        gravado.get("data_liquidacao_efetiva") is not None
        and (existente is None or existente.get("data_liquidacao_efetiva") is None)
    )
    return gravado, chave_pagamento, liquidou_agora


def _limpar_pagamentos_obsoletos(db, chave: dict, chaves_do_lote: set) -> None:
    existentes = _com_filtros(
        db.table("agenda_ur_pagamento").select("tipo_informacao_pagamento,indicador_efeitos_contrato"),
        chave, _CHAVE_UR,
    ).execute().data

    campos_chave = _CHAVE_UR + _CHAVE_PAGAMENTO_EXTRA
    for linha in existentes:
        chave_linha = (linha["tipo_informacao_pagamento"], linha["indicador_efeitos_contrato"])
        if chave_linha in chaves_do_lote:
            continue
        chave_pagamento = dict(chave)
        chave_pagamento["tipo_informacao_pagamento"] = linha["tipo_informacao_pagamento"]
        chave_pagamento["indicador_efeitos_contrato"] = linha["indicador_efeitos_contrato"]
        _com_filtros(db.table("agenda_ur_pagamento").delete(), chave_pagamento, campos_chave).execute()


def upsert_agenda_ur(financiador_id: str, cabecalho: dict, pagamentos: list) -> dict:
    db = get_db(financiador_id)
    chave = {campo: cabecalho[campo] for campo in _CHAVE_UR}
    origem = cabecalho["origem"]
    precedencia_origem(origem)  # valida cedo — erro claro se origem for inválida, em vez de um KeyError tardio num empate
    ocorrido_em = cabecalho["data_hora_ultima_atualizacao"]

    existente = _buscar_um(db, "agenda_ur", chave, _CHAVE_UR)
    criado = existente is None
    if not _deve_sobrescrever(existente, ocorrido_em, origem):
        return {"sobrescrito": False, "agenda_ur": existente, "pagamentos": [], "eventos": []}

    novo = _upsert_cabecalho(db, chave, cabecalho, existente, ocorrido_em)
    eventos = _eventos_do_cabecalho(db, chave, origem, existente, novo, ocorrido_em)

    chaves_do_lote = set()
    pagamentos_gravados = []
    for pagamento in pagamentos:
        gravado, chave_pagamento, liquidou_agora = _upsert_pagamento(db, chave, pagamento, ocorrido_em)
        chaves_do_lote.add((
            chave_pagamento["tipo_informacao_pagamento"],
            chave_pagamento["indicador_efeitos_contrato"],
        ))
        pagamentos_gravados.append(gravado)
        if liquidou_agora:
            valor = gravado.get("valor_liquidacao_efetiva")
            if valor is None:
                valor = gravado.get("valor_a_pagar")
            eventos.append(_registrar_evento(
                db, chave, "LIQUIDACAO", origem, valor, ocorrido_em,
                tipo_informacao_pagamento=chave_pagamento["tipo_informacao_pagamento"],
                indicador_efeitos_contrato=chave_pagamento["indicador_efeitos_contrato"],
            ))

    _limpar_pagamentos_obsoletos(db, chave, chaves_do_lote)

    return {"sobrescrito": True, "agenda_ur": novo, "pagamentos": pagamentos_gravados, "eventos": eventos, "criado": criado}
