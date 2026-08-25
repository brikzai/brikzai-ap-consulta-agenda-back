# agenda-service — Plan 06: Cliente CERC de Consulta (Batch/Online) + Validações A01-A10 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dar ao serviço a capacidade de consultar a agenda de recebíveis na CERC de verdade — `apps/agenda/validation.py` (as 10 validações locais A01-A10 que barram uma chamada inválida antes de sair para a rede) e `services/cerc/client.py` (`consultar_agenda`, que valida, chama `POST /v15/agenda/consultar` batch ou online, mapeia os erros do catálogo 105xxx, e persiste cada UR retornada via `apps.agenda.repository.upsert_agenda_ur`, Plano 05).

**Architecture:** Duas tasks. Task 1 (`validation.py`) não depende de rede — só de `shared.cloudsql_client`/`shared.tenant_config` para as validações que precisam de dado real (domínio de arranjos, rate limit, política de consulta). Task 2 (`client.py`) depende da Task 1 (roda todas as validações antes de qualquer chamada), do Plano 04 (`get_cerc_token`/`invalidate_token`) e do Plano 05 (`upsert_agenda_ur`) — é a peça que efetivamente fala com a CERC e escreve no banco.

**Escopo desta versão:** batch e online juntos, não "batch primeiro, depois online" como a ordem de planos do design doc §16 sugeria em alto nível — o design doc §8 já especifica os dois modos como uma única função parametrizada por `online: bool`, e a diferença de código entre eles é pequena (query param + status inicial de `consulta_agenda`). Fazer os dois agora evita reabrir este mesmo arquivo num plano futuro só para adicionar `online=true`.

**Tech Stack:** `httpx` (chamada HTTP), `respx` (mock da CERC nos testes, mesmo padrão do Plano 04), `python-ulid` (ids técnicos). Nenhuma dependência nova.

**Spec:** `docs/superpowers/specs/2026-08-24-agenda-service-design.md` §7 (política de consulta), §8 (integração CERC). `docs/specs/SPEC-03-consulta-de-agenda-ap005.md` §4 (request/response da consulta), §7.1 (contrato da API interna, usado aqui só como referência de nomes — a view HTTP em si é do Plano 09), §8 (compliance), §10/§10.1 (catálogo de erros e validações locais). Série: plano 6 de ~10.

**Depends on:** `2026-08-24-agenda-plan-04-auth.md` (`get_cerc_token`/`invalidate_token`), `2026-08-25-agenda-plan-05-upsert-repository.md` (`upsert_agenda_ur`).

## Global Constraints

- **Contrato do dict `consulta`** (usado por `validation.py` e `client.py`, é o que o Plano 09 vai montar a partir do corpo HTTP de `POST /api/v1/agendas/consultas`):
  ```python
  {
      "modo": "BATCH" | "ONLINE",                 # obrigatório
      "documento_ufr": str,                        # obrigatório
      "documento_titular": str | None,             # opcional
      "credenciadoras": list[str],                 # obrigatório, não vazia
      "arranjos": list[str],                       # obrigatório, não vazia
      "data_inicio": datetime.date,                # obrigatório
      "data_fim": datetime.date,                   # obrigatório
      "tipo_avaliacao": str | None,                # opcional
      "participante": str | None,                  # opcional
      "carteira": str | None,                      # opcional (obrigatório se A09 exigir)
      "base_autorizativa": {"tipo": "OPTIN" | "CONTRATO", "id": str},  # obrigatório
      "motivo": str,                               # obrigatório
      "ator": str,                                 # obrigatório
      "origem_ip": str | None,                     # opcional
  }
  ```
  `data_inicio`/`data_fim` já vêm como `datetime.date` (quem parseia a string `AAAA-MM-DD` é o chamador, mesma convenção do Plano 05 para `data_hora_ultima_atualizacao`) — este plano não faz parsing de data/hora de request.
- **A01 (zero-pad):** um documento com 10 ou 13 dígitos (faltando um zero à esquerda) é zero-padded para 11 (CPF) ou 14 (CNPJ) antes de validar o dígito verificador — não é tratado como tamanho inválido. Documentos com qualquer outro tamanho são rejeitados por tamanho, sem tentar DV.
- **A07 valida só a FORMA de `base_autorizativa`** (`tipo` em `{OPTIN, CONTRATO}`, `id` não vazio) — verificar se o opt-in está de fato `ATIVO` contra dado real do `optin-service` está bloqueado pela lacuna já registrada no design doc §14 item 2 / §15 risco 4 (o `optin-service` não expõe esse endpoint ainda). Não é uma lacuna nova deste plano.
- **A09 precisa de uma chave nova em `TENANT_{financiador_id}_CONFIG`:** `participante_tipo` (string, valor `"PRESTADOR_SERVICO"` dispara a exigência de `carteira`; qualquer outro valor ou chave ausente não exige). Isso é aditivo — `shared/tenant_config.py` já retorna um dict arbitrário (Plano 03), nenhum código muda lá; só o `.env` de dev/teste continua sem essa chave (ausente = não é Prestador de Serviço, comportamento correto por omissão).
- **Tradução UR → `agenda_ur` é por titular, não por UR.** Uma UR da CERC pode ser fracionada entre titulares (`titulares[]`, SPEC03 §4.3) — cada entrada de `titulares[]` vira **uma linha própria** em `agenda_ur`, usando os valores daquele titular (`valorConstituidoTotal`, `valorBloqueado`, etc. **do titular**, nunca os valores agregados do nível da UR). A única exceção é `valorTotalUR`, que é do nível da UR e se repete igual em todas as linhas de titular (SPEC03 §4.5: "equivale à soma de todas as frações... independentemente do titular").
- **`identificador_cerc_contrato` fica sempre `None` neste plano.** É a coluna 12.16 do arquivo AP005 (SPEC03 §6.4) — não existe no payload da consulta online/batch. Só o Plano 08 (parser de arquivo) a preenche.
- **Forma do corpo de erro da CERC é uma suposição, não confirmada contra a API real.** A SPEC03 não mostra um exemplo de corpo de erro da consulta síncrona — este plano assume `{"erros": [{"codigo": 105003, "mensagem": "..."}]}`, pelo mesmo padrão do envelope de webhook (§5.2, que tem `erros[]`). Ajustar `_tratar_erro_cerc` no primeiro teste real contra homologação, não adivinhar mais que isso agora.
- **`dominio_arranjo` está vazio em qualquer banco real hoje** (§15 risco 11, ainda não resolvido) — A05 vai rejeitar corretamente qualquer arranjo específico (não-`99T`) até aquele risco ser resolvido em outro plano. Não é este plano que semeia a tabela.
- Dinheiro sempre `decimal.Decimal`/`NUMERIC` do banco para dentro; os valores que chegam da CERC em JSON (`float` do `json.loads`) são gravados como vêm — converter para `Decimal` explicitamente é decisão de um plano futuro se o teste de tipo monetário (SPEC04 §1) passar a cobrir dado vindo de fora, não regressão introduzida aqui.

---

### Task 1: `apps/agenda/validation.py`

**Files:**
- Create: `apps/agenda/validation.py`
- Test: `apps/agenda/tests/test_validation.py`

**Interfaces:**
- Consumes: `shared.cloudsql_client.get_db` (Plano 03), `shared.tenant_config.get_tenant_config` (Plano 03).
- Produces: `ValidacaoConsultaError(codigo: str, mensagem: str)`; `validar_consulta(financiador_id: str, consulta: dict) -> None` (roda A01-A10 na ordem, lança na primeira falha); as dez funções individuais (`validar_documento`, `validar_janela_datas`, `validar_lista_nao_vazia`, `validar_sem_mistura_curinga`, `validar_arranjos_no_dominio`, `validar_tipo_avaliacao`, `validar_base_autorizativa`, `validar_rate_limit_online`, `validar_carteira_presente`, `validar_politica_consulta`) — expostas para o Plano 09 poder validar campos isoladamente (ex.: validar um documento assim que ele chega, antes de montar o dict `consulta` inteiro). `services/cerc/client.py` (Task 2 deste plano) chama só `validar_consulta`.

- [ ] **Step 1: Escrever `apps/agenda/validation.py`**

```python
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
```

- [ ] **Step 2: Escrever `apps/agenda/tests/test_validation.py`**

```python
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
```

- [ ] **Step 3: Rodar a suíte**

Run: `pytest apps/agenda/tests/test_validation.py -v`
Expected: PASS em todos os testes (hitam o banco `agenda` real de dev via `TENANT_12345678000199_CONFIG`, mesma estratégia dos planos anteriores — sem mocking do banco).

- [ ] **Step 4: Commit**

```bash
git add apps/agenda/validation.py apps/agenda/tests/test_validation.py
git commit -m "feat: validacoes locais A01-A10 da consulta de agenda"
```

---

### Task 2: `services/cerc/client.py`

**Files:**
- Create: `services/cerc/client.py`
- Test: `services/cerc/tests/test_client.py`

**Interfaces:**
- Consumes: `apps.agenda.validation.validar_consulta` (Task 1); `services.cerc.token_provider.get_cerc_token`/`invalidate_token` (Plano 04); `apps.agenda.repository.upsert_agenda_ur` (Plano 05); `shared.cloudsql_client.get_db` (Plano 03).
- Produces: `consultar_agenda(financiador_id: str, consulta: dict) -> dict` retornando `{"consultaId": str, "status": "COMPLETA" | "PARCIAL", "agendas": list[dict]}` (o `agendas` é o payload cru da CERC, no formato da SPEC03 §4.3 — o Plano 09 decide como expor isso na API interna). `CercConsultaError` (base), `CercConsultaRetentavelError`, `CercConsultaCriticaError`, `CercConsultaInvalidaError` — o Plano 09 traduz cada uma para o HTTP status apropriado (422/403/retentável). O Plano 07 (webhook) não chama esta função — só o fluxo síncrono/online inicial passa por aqui.

- [ ] **Step 1: Escrever `services/cerc/client.py`**

```python
"""Cliente de consulta de agenda da CERC (POST /v15/agenda/consultar,
design doc §8, SPEC03 §4).

consultar_agenda roda as validações A01-A10 (apps.agenda.validation) antes
de qualquer chamada, busca o token CERC (services.cerc.token_provider),
grava a trilha em cerc_requisicao antes de interpretar a resposta, traduz
cada UR (uma linha por titular — ver design doc/plan Global Constraints) e
persiste via apps.agenda.repository.upsert_agenda_ur (origem='SINCRONO').
Mantém consulta_agenda: BATCH fecha em COMPLETA na hora (não há webhook a
esperar); ONLINE abre em PARCIAL — o enriquecimento por webhook é
responsabilidade do Plano 07, que nunca chama esta função.

Mapeamento de erros CERC (catálogo 105xxx, SPEC03 §10):
- 105001 -> sucesso vazio (lista de agendas vazia) — nunca é exceção.
- 105003/105998/105999 -> CercConsultaRetentavelError (retentável; este
  cliente não faz retry automático de erro de negócio, só de token 401).
- 105802 -> CercConsultaCriticaError (não deveria ocorrer — A07 já barra
  antes; se chegar aqui é alerta crítico, design doc §8).
- qualquer outro código -> CercConsultaInvalidaError (equivalente a 422 local).
"""
import os
import uuid
from datetime import datetime, timezone

import httpx
from ulid import ULID

from apps.agenda.repository import upsert_agenda_ur
from apps.agenda.validation import validar_consulta
from services.cerc.token_provider import get_cerc_token, invalidate_token
from shared.cloudsql_client import get_db

_CAMINHO_CONSULTAR = "/v15/agenda/consultar"

_CODIGO_SUCESSO_VAZIO = "105001"
_CODIGO_CRITICO = "105802"
_CODIGOS_RETENTAVEIS = {"105003", "105998", "105999"}


class CercConsultaError(Exception):
    def __init__(self, codigo, mensagem):
        self.codigo = codigo
        self.mensagem = mensagem
        super().__init__(f"{codigo}: {mensagem}")


class CercConsultaRetentavelError(CercConsultaError):
    pass


class CercConsultaCriticaError(CercConsultaError):
    pass


class CercConsultaInvalidaError(CercConsultaError):
    pass


def _corpo_requisicao(consulta: dict) -> dict:
    body = {
        "listaCnpjCredenciadora": consulta["credenciadoras"],
        "documentoUsuarioFinalRecebedor": consulta["documento_ufr"],
        "listaCodigoArranjoPagamento": consulta["arranjos"],
        "dataInicio": consulta["data_inicio"].isoformat(),
        "dataFim": consulta["data_fim"].isoformat(),
    }
    if consulta.get("documento_titular"):
        body["documentoTitular"] = consulta["documento_titular"]
    if consulta.get("tipo_avaliacao"):
        body["tipoAvaliacao"] = consulta["tipo_avaliacao"]
    if consulta.get("participante"):
        body["participante"] = consulta["participante"]
    if consulta.get("carteira"):
        body["carteira"] = consulta["carteira"]
    return body


def _registrar_requisicao(financiador_id: str, correlacao_id: str, request_body: dict, *, http_status, response_body) -> None:
    get_db(financiador_id).table("cerc_requisicao").insert({
        "id": str(ULID()),
        "recurso": "agenda_consultar",
        "correlacao_id": correlacao_id,
        "http_status": http_status,
        "request_body": request_body,
        "response_body": response_body,
    }).execute()


def _tratar_erro_cerc(http_status: int, corpo_resposta) -> list:
    codigo = None
    mensagem = f"HTTP {http_status}"
    if isinstance(corpo_resposta, dict):
        erros = corpo_resposta.get("erros") or []
        if erros:
            codigo = str(erros[0].get("codigo"))
            mensagem = erros[0].get("mensagem", mensagem)

    if codigo == _CODIGO_SUCESSO_VAZIO:
        return []
    if codigo == _CODIGO_CRITICO:
        raise CercConsultaCriticaError(codigo, mensagem)
    if codigo in _CODIGOS_RETENTAVEIS:
        raise CercConsultaRetentavelError(codigo, mensagem)
    raise CercConsultaInvalidaError(codigo or str(http_status), mensagem)


def _chamar_cerc(financiador_id: str, consulta: dict, *, online: bool, tentativa_401: bool = False) -> list:
    token = get_cerc_token(financiador_id)
    body = _corpo_requisicao(consulta)
    correlacao_id = str(uuid.uuid4())

    try:
        response = httpx.post(
            f"{os.environ['CERC_API_BASE_URL']}{_CAMINHO_CONSULTAR}",
            params={"online": "true" if online else "false"},
            json=body,
            headers={"Authorization": f"Bearer {token}", "X-Correlation-Id": correlacao_id},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        _registrar_requisicao(financiador_id, correlacao_id, body, http_status=None, response_body=None)
        raise CercConsultaRetentavelError("105003", f"falha de comunicação com a CERC: {type(exc).__name__}") from None

    if response.status_code == 401 and not tentativa_401:
        invalidate_token(financiador_id)
        return _chamar_cerc(financiador_id, consulta, online=online, tentativa_401=True)

    corpo_resposta = response.json() if response.content else None
    _registrar_requisicao(financiador_id, correlacao_id, body, http_status=response.status_code, response_body=corpo_resposta)

    if response.status_code == 200:
        return corpo_resposta or []

    return _tratar_erro_cerc(response.status_code, corpo_resposta)


def _parse_data_hora(valor: str) -> datetime:
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))


def _traduzir_pagamento(pagamento: dict) -> dict:
    return {
        "tipo_informacao_pagamento": str(pagamento["tipoInformacaoPagamento"]),
        "indicador_efeitos_contrato": pagamento.get("indicadorEfeitosContrato"),
        "identificador_cerc_contrato": None,  # coluna 12.16 — só o Plano 08 (arquivo AP005) preenche
        "regras_divisao": pagamento.get("regrasDivisao"),
        "valor_onerado": pagamento.get("valorOnerado"),
        "valor_constituido_efeito": pagamento.get("valorConstituidoEfeito"),
        "valor_a_pagar": pagamento.get("valorAPagar"),
        "beneficiario": pagamento.get("beneficiario"),
        "data_liquidacao_efetiva": pagamento.get("dataLiquidacaoEfetiva"),
        "valor_liquidacao_efetiva": pagamento.get("valorLiquidacaoEfetiva"),
        "motivo_nao_pagamento": pagamento.get("motivoDeNaoPagamento"),
        "domicilio": pagamento.get("domicilioPagamento") or {},
    }


def _traduzir_ur(agenda: dict, ur: dict) -> list:
    """Uma UR pode ser fracionada entre titulares (SPEC03 §4.3) — cada
    fração vira uma linha própria em agenda_ur, com os valores DO TITULAR
    (nunca os agregados do nível da UR, exceto valorTotalUR — §4.5)."""
    linhas = []
    for titular in ur["titulares"]:
        cabecalho = {
            "entidade_registradora": agenda["entidadeRegistradora"],
            "cnpj_credenciadora": agenda["instituicaoCredenciadora"],
            "codigo_arranjo": agenda["codigoArranjoPagamento"],
            "documento_ufr": agenda["documentoUsuarioFinalRecebedor"],
            "documento_titular": titular["documentoTitular"],
            "data_liquidacao": ur["dataLiquidacao"],
            "constituicao": ur["constituicao"],
            "valor_constituido_total": titular["valorConstituidoTotal"],
            "valor_constituido_antecipacao_pre": titular.get("valorConstituidoAntecipacaoPre", 0),
            "valor_bloqueado": titular.get("valorBloqueado", 0),
            "valor_livre": titular.get("valorLivre", 0),
            "valor_total_ur": ur["valorTotalUR"],
            "carteira": ur.get("carteira"),
            "data_hora_ultima_atualizacao": _parse_data_hora(titular["dataHoraUltimaAtualizacao"]),
            "origem": "SINCRONO",
            "origem_arquivo": None,
        }
        pagamentos = [_traduzir_pagamento(p) for p in titular.get("pagamentos", [])]
        linhas.append((cabecalho, pagamentos))
    return linhas


def _registrar_consulta_agenda(financiador_id: str, consulta: dict, *, online: bool, qtd_urs: int) -> str:
    agora = datetime.now(timezone.utc)
    dados = {
        "id": str(ULID()),
        "modo": consulta["modo"],
        "status": "PARCIAL" if online else "COMPLETA",
        "filtro_ufr": consulta["documento_ufr"],
        "filtro_titular": consulta.get("documento_titular"),
        "filtro_credenciadoras": consulta["credenciadoras"],
        "filtro_arranjos": consulta["arranjos"],
        "filtro_data_inicio": consulta["data_inicio"],
        "filtro_data_fim": consulta["data_fim"],
        "tipo_avaliacao": consulta.get("tipo_avaliacao"),
        "carteira": consulta.get("carteira"),
        "base_autorizativa_tipo": consulta["base_autorizativa"]["tipo"],
        "base_autorizativa_id": consulta["base_autorizativa"]["id"],
        "motivo": consulta["motivo"],
        "ator": consulta["ator"],
        "origem_ip": consulta.get("origem_ip"),
        "qtd_urs_sincrono": qtd_urs,
        "qtd_urs_webhook": 0,
    }
    if not online:
        dados["encerrada_em"] = agora
    if qtd_urs:
        dados["ultima_ur_em"] = agora
    registro = get_db(financiador_id).table("consulta_agenda").insert(dados).execute().data[0]
    return registro["id"]


def consultar_agenda(financiador_id: str, consulta: dict) -> dict:
    validar_consulta(financiador_id, consulta)

    online = consulta["modo"] == "ONLINE"
    agendas = _chamar_cerc(financiador_id, consulta, online=online)

    qtd_urs = 0
    for agenda in agendas:
        for ur in agenda.get("unidadesRecebiveis", []):
            for cabecalho, pagamentos in _traduzir_ur(agenda, ur):
                upsert_agenda_ur(financiador_id, cabecalho, pagamentos)
                qtd_urs += 1

    consulta_id = _registrar_consulta_agenda(financiador_id, consulta, online=online, qtd_urs=qtd_urs)

    return {
        "consultaId": consulta_id,
        "status": "PARCIAL" if online else "COMPLETA",
        "agendas": agendas,
    }
```

- [ ] **Step 2: Escrever `services/cerc/tests/test_client.py`**

```python
from datetime import date, datetime, timezone

import httpx
import pytest
import respx

from apps.agenda import repository, validation
from services.cerc import client
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
CNPJ_VALIDO = "11222333000181"
CPF_VALIDO = "11144477735"
URL_CONSULTAR = "https://ap-homolog.cerc.inf.br/v15/agenda/consultar"

CHAVE_UR_TESTE = {
    "data_liquidacao": "2026-09-20",
    "entidade_registradora": "22246686000196",
    "cnpj_credenciadora": "36216798000150",
    "documento_ufr": CNPJ_VALIDO,
    "documento_titular": CNPJ_VALIDO,
    "codigo_arranjo": "VCC",
}


def _limpar():
    db = get_db(FINANCIADOR_TESTE)
    parcial = {"data_liquidacao": CHAVE_UR_TESTE["data_liquidacao"], "entidade_registradora": CHAVE_UR_TESTE["entidade_registradora"]}
    campos_parciais = ("data_liquidacao", "entidade_registradora")
    repository._com_filtros(db.table("agenda_ur_evento").delete(), parcial, campos_parciais).execute()
    repository._com_filtros(db.table("agenda_ur_pagamento").delete(), parcial, campos_parciais).execute()
    repository._com_filtros(db.table("agenda_ur").delete(), parcial, campos_parciais).execute()
    db.table("consulta_agenda").delete().eq("filtro_ufr", CNPJ_VALIDO).execute()
    db.table("cerc_requisicao").delete().eq("recurso", "agenda_consultar").execute()
    db.table("politica_consulta").delete().eq("motivo", "TESTE-CLIENT").execute()


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    monkeypatch.setenv("CERC_API_BASE_URL", "https://ap-homolog.cerc.inf.br")
    monkeypatch.setattr(client, "get_cerc_token", lambda financiador_id: "token-teste")
    monkeypatch.setattr(client, "invalidate_token", lambda financiador_id: None)

    _limpar()
    db = get_db(FINANCIADOR_TESTE)
    db.table("politica_consulta").insert({
        "id": "pol-teste-client", "motivo": "TESTE-CLIENT",
        "modos_permitidos": ["BATCH", "ONLINE"], "ativo": True,
    }).execute()
    yield
    _limpar()


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
        "motivo": "TESTE-CLIENT",
        "ator": "teste@teste.com",
        "origem_ip": None,
    }
    base.update(overrides)
    return base


def _titular(documento: str, valor: float, **overrides):
    base = {
        "documentoTitular": documento,
        "valorConstituidoTotal": valor,
        "valorConstituidoAntecipacaoPre": 0.0,
        "valorBloqueado": 0.0,
        "valorLivre": valor,
        "dataHoraUltimaAtualizacao": "2026-09-19T10:00:00.000Z",
        "pagamentos": [],
    }
    base.update(overrides)
    return base


def _resposta_cerc(titulares=None, **overrides_ur):
    ur = {
        "dataLiquidacao": "2026-09-20",
        "constituicao": "1",
        "valorConstituidoTotal": 1000.0,
        "valorConstituidoAntecipacaoPre": 0.0,
        "valorBloqueado": 0.0,
        "valorLivre": 1000.0,
        "valorTotalUR": 1000.0,
        "dataHoraUltimaAtualizacao": "2026-09-19T10:00:00.000Z",
        "pagamentos": [],
        "titulares": titulares if titulares is not None else [_titular(CNPJ_VALIDO, 1000.0)],
    }
    ur.update(overrides_ur)
    return [{
        "entidadeRegistradora": "22246686000196",
        "instituicaoCredenciadora": "36216798000150",
        "codigoArranjoPagamento": "VCC",
        "documentoUsuarioFinalRecebedor": CNPJ_VALIDO,
        "indicadoresConsistencia": [],
        "unidadesRecebiveis": [ur],
    }]


@respx.mock
def test_consultar_agenda_batch_persiste_ur_e_fecha_completa():
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc()))

    resultado = client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base(modo="BATCH"))

    assert resultado["status"] == "COMPLETA"
    assert resultado["consultaId"]

    db = get_db(FINANCIADOR_TESTE)
    ur_gravada = repository._com_filtros(db.table("agenda_ur").select("*"), CHAVE_UR_TESTE, repository._CHAVE_UR).execute().data
    assert len(ur_gravada) == 1
    assert ur_gravada[0]["origem"] == "SINCRONO"

    consulta = db.table("consulta_agenda").select("*").eq("id", resultado["consultaId"]).execute().data[0]
    assert consulta["status"] == "COMPLETA"
    assert consulta["qtd_urs_sincrono"] == 1
    assert consulta["encerrada_em"] is not None


@respx.mock
def test_consultar_agenda_online_abre_parcial():
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc()))

    resultado = client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base(modo="ONLINE"))

    assert resultado["status"] == "PARCIAL"
    db = get_db(FINANCIADOR_TESTE)
    consulta = db.table("consulta_agenda").select("*").eq("id", resultado["consultaId"]).execute().data[0]
    assert consulta["status"] == "PARCIAL"
    assert consulta["encerrada_em"] is None


@respx.mock
def test_consultar_agenda_persiste_uma_linha_por_titular():
    titulares = [_titular(CNPJ_VALIDO, 500.0), _titular(CPF_VALIDO, 500.0)]
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc(titulares=titulares)))

    client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    db = get_db(FINANCIADOR_TESTE)
    linhas = (
        db.table("agenda_ur").select("documento_titular,valor_total_ur")
        .eq("data_liquidacao", "2026-09-20")
        .eq("entidade_registradora", "22246686000196")
        .execute().data
    )
    assert {r["documento_titular"] for r in linhas} == {CNPJ_VALIDO, CPF_VALIDO}
    assert all(r["valor_total_ur"] == 1000 for r in linhas)  # valorTotalUR é do nível da UR, igual pros dois titulares


@respx.mock
def test_consultar_agenda_codigo_105001_retorna_lista_vazia_sem_erro():
    respx.post(URL_CONSULTAR).mock(
        return_value=httpx.Response(422, json={"erros": [{"codigo": 105001, "mensagem": "nada encontrado"}]})
    )

    resultado = client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    assert resultado["agendas"] == []
    assert resultado["status"] == "COMPLETA"


@respx.mock
def test_consultar_agenda_codigo_105003_e_retentavel():
    respx.post(URL_CONSULTAR).mock(
        return_value=httpx.Response(422, json={"erros": [{"codigo": 105003, "mensagem": "falha na registradora"}]})
    )

    with pytest.raises(client.CercConsultaRetentavelError) as exc_info:
        client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())
    assert exc_info.value.codigo == "105003"


@respx.mock
def test_consultar_agenda_codigo_105802_e_critico():
    respx.post(URL_CONSULTAR).mock(
        return_value=httpx.Response(403, json={"erros": [{"codigo": 105802, "mensagem": "opt-in não encontrado"}]})
    )

    with pytest.raises(client.CercConsultaCriticaError):
        client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())


@respx.mock
def test_consultar_agenda_erro_de_validacao_gera_erro_invalido():
    respx.post(URL_CONSULTAR).mock(
        return_value=httpx.Response(422, json={"erros": [{"codigo": 105009, "mensagem": "arranjo inválido"}]})
    )

    with pytest.raises(client.CercConsultaInvalidaError) as exc_info:
        client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())
    assert exc_info.value.codigo == "105009"


@respx.mock
def test_consultar_agenda_bloqueia_localmente_antes_de_chamar_cerc():
    route = respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=[]))

    with pytest.raises(validation.ValidacaoConsultaError):
        client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base(motivo="MOTIVO-SEM-POLITICA-CLIENT"))

    assert route.call_count == 0


@respx.mock
def test_consultar_agenda_retenta_uma_vez_em_401(monkeypatch):
    chamadas_invalidacao = []
    monkeypatch.setattr(client, "invalidate_token", lambda financiador_id: chamadas_invalidacao.append(financiador_id))

    route = respx.post(URL_CONSULTAR).mock(
        side_effect=[
            httpx.Response(401, json={"erro": "token expirado"}),
            httpx.Response(200, json=_resposta_cerc()),
        ]
    )

    resultado = client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    assert route.call_count == 2
    assert chamadas_invalidacao == [FINANCIADOR_TESTE]
    assert resultado["status"] == "COMPLETA"


@respx.mock
def test_consultar_agenda_grava_cerc_requisicao_antes_de_interpretar_resposta():
    respx.post(URL_CONSULTAR).mock(return_value=httpx.Response(200, json=_resposta_cerc()))

    client.consultar_agenda(FINANCIADOR_TESTE, _consulta_base())

    db = get_db(FINANCIADOR_TESTE)
    requisicoes = db.table("cerc_requisicao").select("*").eq("recurso", "agenda_consultar").execute().data
    assert len(requisicoes) == 1
    assert requisicoes[0]["http_status"] == 200
```

- [ ] **Step 3: Rodar a suíte**

Run: `pytest services/cerc/tests/test_client.py -v`
Expected: PASS em todos os testes (CERC mockada via `respx`; token mockado via `monkeypatch` no próprio `client` — isola a lógica de consulta da lógica de OAuth2, já coberta pelos testes do Plano 04; banco real de dev, mesma estratégia dos planos anteriores).

- [ ] **Step 4: Commit**

```bash
git add services/cerc/client.py services/cerc/tests/test_client.py
git commit -m "feat: cliente CERC de consulta de agenda (batch/online) com mapeamento de erros 105xxx"
```

---

## Self-Review Notes

- **Spec coverage:** SPEC03 §10.1 (A01-A10) → `apps/agenda/validation.py`, uma função por letra, testada individualmente e em conjunto (`validar_consulta`). Design doc §8 → `services/cerc/client.py`: token com retry de 401 (bullet 2), `cerc_requisicao` gravado antes de interpretar a resposta (bullet 3), batch e online com origem `SINCRONO` (bullets 4-5), mapeamento 105001/105003/105999/105998/105802 (bullet 6, com 105998 tratado como retentável igual a 105003/105999 já que este plano não implementa a fila de reprocessamento — só devolve um erro retentável para quem chamar decidir). SPEC03 §4.3/§4.5 (tradução por titular, `valorTotalUR` no nível da UR) → `_traduzir_ur`, testado com 1 e 2 titulares.
- **Placeholder scan:** nenhum — todo step tem código executável completo (`from datetime import datetime, timezone` no topo de `client.py` cobre o `timezone.utc` usado em `_registrar_consulta_agenda`).
- **Type consistency:** `validar_consulta(financiador_id: str, consulta: dict) -> None` e `consultar_agenda(financiador_id: str, consulta: dict) -> dict` compartilham exatamente o mesmo formato de `consulta` (Global Constraints) — nenhuma tradução de nomes de campo entre as duas funções. `upsert_agenda_ur(financiador_id, cabecalho, pagamentos)` (Plano 05) é chamado com as chaves exatas de `agenda_ur`/`agenda_ur_pagamento` (mesmos nomes usados nos testes do Plano 05).
- **Decisão explícita de escopo:** batch e online implementados juntos nesta task (ver seção "Escopo desta versão" no cabeçalho), não faseados como a redação de alto nível do design doc §16 sugeriu — motivo registrado ali.
- **Assunção não confirmada, registrada para o primeiro teste real:** a forma exata do corpo de erro da CERC (`{"erros": [...]}`) é inferida do padrão do webhook, não confirmada contra a consulta síncrona real — ver Global Constraints.
- **Fora de escopo deste plano:** fila de reprocessamento de 105998 (design doc diz "enfileirar e reprocessar", este plano só devolve `CercConsultaRetentavelError` e deixa o enfileiramento para quando existir um consumidor — Plano 07 ou um plano de jobs); seed de `dominio_arranjo` (§15 risco 11); endpoint HTTP de `optin-service` para A07 real (§14 item 2 / §15 risco 4); a view HTTP `POST /api/v1/agendas/consultas` em si (Plano 09).

**Next:** `2026-08-24-agenda-plan-07-webhook.md` (webhook + correlação + Pub/Sub + job de completude) — ainda não escrito.
