"""Correlação webhook <-> consulta (SPEC03 §5.4) — o payload do webhook não
carrega um identificador da consulta que o originou, então a correlação é
reconstruída pela chave de negócio: (instituicaoCredenciadora,
codigoArranjoPagamento, documentoUsuarioFinalRecebedor, documentoTitular,
dataLiquidacao), casada contra consultas ONLINE ainda em PARCIAL, tratando
'99T' no filtro como universo e a janela de datas como intervalo fechado.
"""
from datetime import date

from shared.cloudsql_client import get_db


def _lista_contem(lista, valor: str) -> bool:
    return "99T" in lista or valor in lista


def encontrar_consultas_candidatas(financiador_id: str, evento: dict) -> list:
    db = get_db(financiador_id)
    candidatas = (
        db.table("consulta_agenda").select("*")
        .eq("status", "PARCIAL").eq("modo", "ONLINE")
        .execute().data
    )

    documento_ufr = evento["documentoUsuarioFinalRecebedor"]
    documento_titular = evento.get("documentoTitular")
    credenciadora = evento["instituicaoCredenciadora"]
    arranjo = evento["codigoArranjoPagamento"]
    data_liquidacao = date.fromisoformat(evento["dataLiquidacao"])

    casadas = []
    for consulta in candidatas:
        if consulta["filtro_ufr"] != documento_ufr:
            continue
        if consulta["filtro_titular"] and documento_titular and consulta["filtro_titular"] != documento_titular:
            continue
        if not _lista_contem(consulta["filtro_credenciadoras"], credenciadora):
            continue
        if not _lista_contem(consulta["filtro_arranjos"], arranjo):
            continue
        if not (consulta["filtro_data_inicio"] <= data_liquidacao <= consulta["filtro_data_fim"]):
            continue
        casadas.append(consulta)
    return casadas
