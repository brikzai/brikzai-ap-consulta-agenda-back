# agenda-service — Plan 08: Ingestão de Arquivo AP005/AP005A/AP005B — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingerir os arquivos `CERC-AP005`/`CERC-AP005A`/`CERC-AP005B` (design doc §9, SPEC03 §6) — detectar leiaute por contagem de colunas, traduzir cada linha para o formato de `upsert_agenda_ur` (origem `ARQUIVO`), agrupar linhas consecutivas da mesma UR num único upsert, rejeitar linhas inválidas sem abortar o arquivo, e expor tudo isso por um endpoint HTTP interno — deixando a conexão real (SFTP/Bucket/Connect:Direct) para um plano futuro, já que essa infra é declarada "a definir" pelo design doc (§14 item 4).

**Architecture:** Três tasks. Task 1 é o parser puro (`apps/agenda/parser_ap005.py`) — sem I/O, sem banco, 100% testável com listas de strings construídas à mão. Task 2 é a orquestração de ingestão (`apps/agenda/importar_ap005.py`) — idempotência, descompactação gzip em streaming, agrupamento por chave de UR e chamada a `upsert_agenda_ur` (Plano 05), gravação de rejeitadas e contadores em `arquivo_agenda_processado`; testado contra o banco `agenda` real de dev (mesma prática de `test_repository.py`/`test_views_completude.py`). Task 3 é o endpoint HTTP interno (`POST /api/v1/jobs/importar-ap005/{financiador_id}`), protegido pelo mesmo OIDC dos outros jobs (`shared/pubsub_auth.verificar_push_oidc`), que recebe o arquivo já em mãos via corpo bruto da requisição (streaming, nunca `request.body`) e delega para a Task 2.

**Tech Stack:** Só biblioteca padrão (`csv`, `gzip`, `io`, `re`) — nenhuma dependência nova em `requirements.txt`.

**Spec:** `docs/superpowers/specs/2026-08-24-agenda-service-design.md` §9 (ingestão de arquivo), §14 item 4 (conexão real a definir), §15 riscos 1, 5, 6, 9, 13 (gaps herdados que este plano resolve ou carrega explicitamente). `docs/specs/SPEC-03-consulta-de-agenda-ap005.md` §6 (layout completo do canal de arquivo, detecção de leiaute, domínio `tipoInformacaoPagamento`, requisitos do ingestor). Referência de implementação: `services/cerc/client.py` (`_traduzir_ur`/`_traduzir_pagamento`, Plano 06) e `apps/agenda/views.py` (`_traduzir_evento_webhook`, Plano 07) — mesma convenção de nomes de campo (`entidade_registradora`, `cnpj_credenciadora`, ... e `domicilio` em camelCase interno), adaptada de JSON para colunas de CSV. Série: plano 8 de ~10.

**Depends on:** `2026-08-25-agenda-plan-05-upsert-repository.md` (`upsert_agenda_ur`, `_CHAVE_UR`), `2026-08-25-agenda-plan-07-webhook.md` (`shared/pubsub_auth.verificar_push_oidc`, mesmo mecanismo de proteção reaproveitado aqui).

## Global Constraints

- **Decisão de escopo (confirmada com o usuário antes deste plano): sem poller real.** A conexão real de chegada do arquivo (SFTP/Bucket/Connect:Direct, design doc §14 item 4) permanece indefinida — este plano constrói só o parser e a função de ingestão, expostos por um endpoint HTTP interno que recebe o arquivo já em mãos. Um plano futuro liga isso a Cloud Scheduler + o canal real, quando a credencial existir. Nenhuma dependência nova (`google-cloud-storage` etc.) entra em `requirements.txt` por causa disso.
- **Decisão de layout (confirmada com o usuário antes deste plano; permanece como ponto a confirmar com a CERC, SPEC03 §13): uma linha de CSV por pagamento.** O arquivo real repete as colunas 1-11 e 13-16 da UR a cada linha, variando só o bloco 12.x — mesmo modelo que a API síncrona/webhook já usam para lista de pagamentos (lá é uma lista JSON; aqui são N linhas de arquivo). `apps/agenda/importar_ap005.py` agrupa linhas **consecutivas** da mesma chave de UR antes de chamar `upsert_agenda_ur` (que exige a lista completa de pagamentos do lote, nunca um delta — `repository.py`, docstring do módulo). Se o arquivo real não vier agrupado assim, um grupo não-consecutivo da mesma UR é tratado como upsert independente e apaga os pagamentos do grupo anterior que não repetir — mesma classe de gap best-effort do design doc §15 risco 1, carregada explicitamente aqui até haver um arquivo real para confirmar a ordenação.
- **Detecção de leiaute por contagem FÍSICA de colunas do CSV**, não pela numeração de campos do SPEC03 §6.2 (que trata "coluna 12" como um bloco nomeado de 16 subcampos — aqui cada subcampo é uma célula própria do CSV): `<= 4` colunas → `REDUZIDO` (arquivo "sem agenda", §6.2, resultado válido, nunca rejeitado); `15` colunas → `SEM_PAGAMENTO` (UR sem bloco de pagamento — "baixa de UR sem pagamentos a fazer"); `30` colunas → `COM_PAGAMENTO_SEM_1216` (sem a coluna 12.16, arquivos anteriores a 03/11/2025, §6.4); `31` colunas → `COM_PAGAMENTO_COM_1216`. Qualquer outra contagem é `LinhaInvalidaError`.
- **Resolução do design doc §15 risco 5 (descompasso de vocabulário):** `agenda_ur.origem_arquivo` passa a gravar o **mesmo valor com prefixo** que `arquivo_agenda_processado.tipo_leiaute` (`CERC-AP005`/`CERC-AP005A`/`CERC-AP005B`), sem conversão. Nenhuma migração necessária — nenhum plano anterior gravou dado real nessa coluna (`origem_arquivo` sempre foi `None` nos Planos 06/07).
- **Resolução do design doc §15 risco 6:** `agenda_ur_pagamento.indicador_efeitos_contrato` (coluna 12.14 do arquivo, "indicador de ordem do efeito") é `NOT NULL DEFAULT ''` no schema — o parser traduz campo ausente/vazio para `""`, nunca para `None` (`campo or ""`, nunca `campo or None`).
- **Resolução do design doc §15 risco 13 (volume/throughput):** decisão (a) — usar `upsert_agenda_ur` como está, sem caminho de escrita em lote alternativo, mesma filosofia "não otimizar antes de medir" já aplicada em outros pontos deste design. Reavaliar se o volume real de um arquivo AP005 de produção (SPEC04 projeta milhões de linhas/dia) se mostrar lento.
- **`motivo_nao_pagamento` sempre `None` no caminho de arquivo** — essa coluna não tem campo correspondente no leiaute do arquivo (SPEC03 §6.2), só existe no payload da API/webhook.
- **Coluna 1 (`referência externa`) não é mapeada** — mesmo gap do design doc §14 item 5 (`referenciaExterna` sem coluna correspondente no schema atual). Não é uma lacuna nova deste plano.
- **Assunções a confirmar com um arquivo real (documentar, não bloquear):** separador decimal `.` (ponto, não vírgula — mesma convenção ISO que as datas do arquivo já usam, `AAAA-MM-DD`) e encoding `UTF-8` (`errors="replace"` para nunca abortar o arquivo inteiro por um byte inválido).
- **Streaming obrigatório (SPEC03 §6.6):** a função de ingestão nunca lê o arquivo inteiro em memória — itera linha a linha via `csv.reader` sobre um `io.TextIOWrapper`, e AP005A/AP005B (`.csv.gz`) são descompactados via `gzip.GzipFile(fileobj=..., mode="rb")`, também em streaming.
- **Idempotência por `(tipo_leiaute, ident_ic, data_req, seq)` (SPEC03 §6.6):** checagem de EXISTÊNCIA da linha em `arquivo_agenda_processado` antes de processar (não de `concluido_em IS NULL`) — qualquer linha existente, mesmo de uma tentativa anterior incompleta, torna a chamada um no-op. Reprocessar um arquivo travado a meio caminho exige apagar manualmente a linha de controle — gap aceito conscientemente, mesma filosofia de outros pontos deste design.
- **Taxa de rejeição:** alertar (log `warning`) quando rejeitadas/lidas exceder 0,5% (SPEC03 §6.6) — não é um bloqueio, só um log estruturado.

---

### Task 1: Parser puro (`apps/agenda/parser_ap005.py`)

**Files:**
- Create: `apps/agenda/parser_ap005.py`
- Test: `apps/agenda/tests/test_parser_ap005.py`

**Interfaces:**
- Produces: `parse_nome_arquivo(nome_arquivo: str) -> dict` (`{tipo_leiaute, ident_ic, data_req: date, seq: int, comprimido: bool}`); `detectar_layout(n_campos: int) -> str` (`"REDUZIDO"|"SEM_PAGAMENTO"|"COM_PAGAMENTO_SEM_1216"|"COM_PAGAMENTO_COM_1216"`); `traduzir_linha(campos: list[str], tipo_leiaute: str) -> tuple[dict, dict | None] | None` (retorna `None` para leiaute reduzido); `NomeArquivoInvalidoError`, `LinhaInvalidaError`. Consumido pela Task 2.

- [ ] **Step 1: Escrever `apps/agenda/tests/test_parser_ap005.py`**

```python
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
```

- [ ] **Step 2: Rodar os testes e confirmar que falham** (módulo ainda não existe)

Run: `pytest apps/agenda/tests/test_parser_ap005.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'apps.agenda.parser_ap005'`

- [ ] **Step 3: Escrever `apps/agenda/parser_ap005.py`**

```python
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
```

- [ ] **Step 4: Rodar os testes e confirmar que passam**

Run: `pytest apps/agenda/tests/test_parser_ap005.py -v`
Expected: PASS (13 testes)

- [ ] **Step 5: Commit**

```bash
git add apps/agenda/parser_ap005.py apps/agenda/tests/test_parser_ap005.py
git commit -m "feat: parser do arquivo AP005/AP005A/AP005B (Plano 08, Task 1)"
```

---

### Task 2: Orquestração de ingestão (`apps/agenda/importar_ap005.py`)

**Files:**
- Create: `apps/agenda/importar_ap005.py`
- Test: `apps/agenda/tests/test_importar_ap005.py`

**Interfaces:**
- Consumes: `apps.agenda.parser_ap005.parse_nome_arquivo`, `.traduzir_linha`, `.LinhaInvalidaError`, `.NomeArquivoInvalidoError` (Task 1); `apps.agenda.repository.upsert_agenda_ur`, `._CHAVE_UR` (Plano 05); `shared.cloudsql_client.get_db` (Plano 03).
- Produces: `importar_arquivo(financiador_id: str, nome_arquivo: str, conteudo) -> dict` — `conteudo` é qualquer objeto com `.read(n)` (streaming); retorna `{"ja_processado": bool, "tipo_leiaute", "ident_ic", "data_req", "seq", "comprimido", "linhas_lidas"?, "linhas_ok"?, "linhas_rejeitadas"?}` (as três últimas chaves ausentes quando `ja_processado=True`). Consumido pela Task 3.

- [ ] **Step 1: Escrever `apps/agenda/tests/test_importar_ap005.py`**

```python
import csv
import gzip
import io
from datetime import date

import pytest

from apps.agenda import importar_ap005
from apps.agenda.parser_ap005 import parse_nome_arquivo
from apps.agenda.repository import _CHAVE_UR, _com_filtros
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
NOME_ARQUIVO = "CERC-AP005_11112222_20260920_0000001_ret.csv"
NOME_ARQUIVO_GZ = "CERC-AP005A_11112222_20260921_0000002_ret.csv.gz"

_CHAVE_TESTE = {
    "data_liquidacao": date(2026, 9, 20),
    "entidade_registradora": "22246686000196",
    "cnpj_credenciadora": "01027058000191",
    "documento_ufr": "77777777000177",
    "documento_titular": "77777777000177",
    "codigo_arranjo": "VCC",
}


def _linha(*, tipo_informacao_pagamento="6", com_1216=True, identificador="CTR-TESTE", documento_titular=None):
    titular = documento_titular or _CHAVE_TESTE["documento_titular"]
    base = [
        "REF-001", _CHAVE_TESTE["entidade_registradora"], _CHAVE_TESTE["cnpj_credenciadora"],
        _CHAVE_TESTE["documento_ufr"], _CHAVE_TESTE["codigo_arranjo"], "2026-09-20",
        titular, "1", "1000.00", "0", "0",
    ]
    bloco = [
        titular, "CC", "001", "00000000", "1234", "123456-7",
        "500.00", "", "", "", "", "", tipo_informacao_pagamento, "", "",
    ]
    if com_1216:
        bloco.append(identificador)
    cauda = ["", "0", "1000.00", "2026-09-20T10:00:00Z"]
    return base + bloco + cauda


def _csv_bytes(linhas, *, comprimido=False) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer).writerows(linhas)
    dados = buffer.getvalue().encode("utf-8")
    if not comprimido:
        return dados
    saida = io.BytesIO()
    with gzip.GzipFile(fileobj=saida, mode="wb") as f:
        f.write(dados)
    return saida.getvalue()


def _apagar_meta(nome_arquivo):
    meta = parse_nome_arquivo(nome_arquivo)
    get_db(FINANCIADOR_TESTE).table("arquivo_agenda_processado").delete().eq(
        "tipo_leiaute", meta["tipo_leiaute"]).eq("ident_ic", meta["ident_ic"]
    ).eq("data_req", meta["data_req"]).eq("seq", meta["seq"]).execute()


@pytest.fixture(autouse=True)
def _limpar():
    db = get_db(FINANCIADOR_TESTE)

    def _fazer():
        _com_filtros(db.table("agenda_ur_pagamento").delete(), _CHAVE_TESTE, _CHAVE_UR).execute()
        _com_filtros(db.table("agenda_ur").delete(), _CHAVE_TESTE, _CHAVE_UR).execute()
        db.table("agenda_ur_rejeitada").delete().eq("arquivo", NOME_ARQUIVO).execute()
        _apagar_meta(NOME_ARQUIVO)
        _apagar_meta(NOME_ARQUIVO_GZ)

    _fazer()
    yield
    _fazer()


def test_importa_um_pagamento():
    conteudo = io.BytesIO(_csv_bytes([_linha()]))
    resultado = importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO, conteudo)

    assert resultado["ja_processado"] is False
    assert resultado["linhas_lidas"] == 1
    assert resultado["linhas_ok"] == 1
    assert resultado["linhas_rejeitadas"] == 0

    db = get_db(FINANCIADOR_TESTE)
    ur = _com_filtros(db.table("agenda_ur").select("*"), _CHAVE_TESTE, _CHAVE_UR).execute().data[0]
    assert ur["origem"] == "ARQUIVO"
    assert ur["origem_arquivo"] == "CERC-AP005"
    pagamentos = _com_filtros(db.table("agenda_ur_pagamento").select("*"), _CHAVE_TESTE, _CHAVE_UR).execute().data
    assert len(pagamentos) == 1
    assert pagamentos[0]["identificador_cerc_contrato"] == "CTR-TESTE"


def test_agrupa_linhas_consecutivas_da_mesma_ur_num_unico_upsert():
    linhas = [
        _linha(tipo_informacao_pagamento="6", identificador="CTR-1"),
        _linha(tipo_informacao_pagamento="1", identificador="CTR-2"),
    ]
    conteudo = io.BytesIO(_csv_bytes(linhas))
    resultado = importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO, conteudo)

    assert resultado["linhas_ok"] == 2
    db = get_db(FINANCIADOR_TESTE)
    pagamentos = _com_filtros(db.table("agenda_ur_pagamento").select("*"), _CHAVE_TESTE, _CHAVE_UR).execute().data
    assert {p["tipo_informacao_pagamento"] for p in pagamentos} == {"6", "1"}


def test_idempotente_segunda_chamada_nao_reprocessa():
    conteudo1 = io.BytesIO(_csv_bytes([_linha()]))
    importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO, conteudo1)

    conteudo2 = io.BytesIO(_csv_bytes([_linha(identificador="CTR-DIFERENTE")]))
    resultado = importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO, conteudo2)

    assert resultado["ja_processado"] is True
    db = get_db(FINANCIADOR_TESTE)
    pagamentos = _com_filtros(db.table("agenda_ur_pagamento").select("*"), _CHAVE_TESTE, _CHAVE_UR).execute().data
    assert pagamentos[0]["identificador_cerc_contrato"] == "CTR-TESTE"


def test_linha_invalida_vai_para_rejeitada_e_arquivo_continua():
    linha_invalida = _linha(tipo_informacao_pagamento="9")
    linha_valida = _linha(documento_titular="88888888000188")
    conteudo = io.BytesIO(_csv_bytes([linha_invalida, linha_valida]))

    resultado = importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO, conteudo)

    assert resultado["linhas_lidas"] == 2
    assert resultado["linhas_rejeitadas"] == 1
    assert resultado["linhas_ok"] == 1

    db = get_db(FINANCIADOR_TESTE)
    rejeitadas = db.table("agenda_ur_rejeitada").select("*").eq("arquivo", NOME_ARQUIVO).execute().data
    assert len(rejeitadas) == 1
    assert rejeitadas[0]["linha"] == 1
    assert "tipoInformacaoPagamento" in rejeitadas[0]["motivo"]

    chave_valida = {**_CHAVE_TESTE, "documento_titular": "88888888000188"}
    ur = _com_filtros(db.table("agenda_ur").select("*"), chave_valida, _CHAVE_UR).execute().data
    assert len(ur) == 1
    _com_filtros(db.table("agenda_ur_pagamento").delete(), chave_valida, _CHAVE_UR).execute()
    _com_filtros(db.table("agenda_ur").delete(), chave_valida, _CHAVE_UR).execute()


def test_layout_reduzido_conta_como_linha_ok_sem_gravar_ur():
    conteudo = io.BytesIO(_csv_bytes([["REF-001", "2026-09-20T10:00:00Z"]]))
    resultado = importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO, conteudo)
    assert resultado["linhas_ok"] == 1
    assert resultado["linhas_rejeitadas"] == 0


def test_descompacta_gzip_em_streaming():
    conteudo = io.BytesIO(_csv_bytes([_linha()], comprimido=True))
    resultado = importar_ap005.importar_arquivo(FINANCIADOR_TESTE, NOME_ARQUIVO_GZ, conteudo)
    assert resultado["linhas_ok"] == 1
    assert resultado["tipo_leiaute"] == "CERC-AP005A"
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `pytest apps/agenda/tests/test_importar_ap005.py -v`
Expected: FAIL com `ModuleNotFoundError: No module named 'apps.agenda.importar_ap005'`

- [ ] **Step 3: Escrever `apps/agenda/importar_ap005.py`**

```python
"""Ingestão de arquivo AP005/AP005A/AP005B (design doc §9, SPEC03 §6.6).

importar_arquivo(financiador_id, nome_arquivo, conteudo) é o ponto de
entrada — conteudo é qualquer objeto binário com .read() (streaming,
nunca materializa o arquivo inteiro em memória: SPEC03 §6.6). AP005A/AP005B
chegam em .gzip (§6.5), descompactados por streaming via gzip.GzipFile.

Idempotência por (tipo_leiaute, ident_ic, data_req, seq) — SPEC03 §6.6: se
já existe uma linha em arquivo_agenda_processado (de QUALQUER tentativa
anterior, mesmo incompleta), o arquivo é considerado já reivindicado e a
chamada é um no-op. Reprocessar um arquivo travado a meio caminho exige
apagar manualmente a linha de controle — gap aceito conscientemente (mesma
filosofia de "não resolver antes de medir" do design doc §15).

Agrupamento por UR: o arquivo real da CERC repete as colunas 1-11/13-16 a
cada linha de pagamento da mesma UR (Plano 08, ver parser_ap005.py) — este
módulo assume que essas linhas vêm CONSECUTIVAS no arquivo (a mesma chave
não reaparece depois que outra chave começou) para poder agrupar em
streaming, sem materializar o arquivo inteiro. Se o arquivo real não vier
agrupado assim, cada grupo não-consecutivo é tratado como upsert
independente e o mais recente apaga (via
upsert_agenda_ur/_limpar_pagamentos_obsoletos) os pagamentos do grupo
anterior que não repetir — mesma classe de gap best-effort do design doc
§15 risco 1, carregado explicitamente aqui até haver um arquivo real para
confirmar a ordenação (SPEC03 §13).
"""
import csv
import gzip
import io
import logging
from datetime import datetime, timezone

from apps.agenda import parser_ap005
from apps.agenda.repository import _CHAVE_UR, upsert_agenda_ur
from shared.cloudsql_client import get_db

logger = logging.getLogger(__name__)

_LIMIAR_ALERTA_REJEICAO = 0.005


def _ja_processado(db, meta: dict) -> bool:
    existentes = (
        db.table("arquivo_agenda_processado").select("tipo_leiaute")
        .eq("tipo_leiaute", meta["tipo_leiaute"]).eq("ident_ic", meta["ident_ic"])
        .eq("data_req", meta["data_req"]).eq("seq", meta["seq"]).execute()
    )
    return bool(existentes.data)


def _registrar_rejeitada(db, nome_arquivo: str, numero_linha: int, campos: list, motivo: str) -> None:
    db.table("agenda_ur_rejeitada").insert({
        "origem": "ARQUIVO",
        "arquivo": nome_arquivo,
        "linha": numero_linha,
        "conteudo": ",".join(campos),
        "motivo": motivo,
    }).execute()


def _abrir_texto(conteudo, comprimido: bool):
    stream = gzip.GzipFile(fileobj=conteudo, mode="rb") if comprimido else conteudo
    return io.TextIOWrapper(stream, encoding="utf-8", errors="replace", newline="")


class _Agrupador:
    """Acumula linhas consecutivas da mesma chave de UR e dispara
    upsert_agenda_ur uma vez por grupo, nunca uma vez por linha — o
    contrato de upsert_agenda_ur exige a lista COMPLETA de pagamentos da
    UR no lote (repository.py, docstring do módulo)."""

    def __init__(self, financiador_id: str, nome_arquivo: str, db):
        self._financiador_id = financiador_id
        self._nome_arquivo = nome_arquivo
        self._db = db
        self._chave = None
        self._cabecalho = None
        self._linhas_do_grupo: list = []  # [(numero_linha, campos_brutos, pagamento_ou_none)]
        self.linhas_ok = 0
        self.linhas_rejeitadas = 0

    def adicionar(self, numero_linha: int, campos: list, cabecalho: dict, pagamento) -> None:
        chave = tuple(cabecalho[campo] for campo in _CHAVE_UR)
        if self._chave is not None and chave != self._chave:
            self._flush()
        self._chave = chave
        self._cabecalho = cabecalho
        self._linhas_do_grupo.append((numero_linha, campos, pagamento))

    def _flush(self) -> None:
        if self._cabecalho is None:
            return
        pagamentos = [p for (_, _, p) in self._linhas_do_grupo if p is not None]
        try:
            upsert_agenda_ur(self._financiador_id, self._cabecalho, pagamentos)
            self.linhas_ok += len(self._linhas_do_grupo)
        except Exception as exc:
            logger.exception(
                "[ImportarAP005] Falha ao gravar UR (financiador=%s, arquivo=%s, linhas=%s)",
                self._financiador_id, self._nome_arquivo, [n for (n, _, _) in self._linhas_do_grupo],
            )
            for numero_linha, campos, _ in self._linhas_do_grupo:
                _registrar_rejeitada(self._db, self._nome_arquivo, numero_linha, campos, f"{type(exc).__name__}: {exc}")
            self.linhas_rejeitadas += len(self._linhas_do_grupo)
        self._chave = None
        self._cabecalho = None
        self._linhas_do_grupo = []

    def finalizar(self) -> None:
        self._flush()


def importar_arquivo(financiador_id: str, nome_arquivo: str, conteudo) -> dict:
    meta = parser_ap005.parse_nome_arquivo(nome_arquivo)
    db = get_db(financiador_id)

    if _ja_processado(db, meta):
        logger.info("[ImportarAP005] Arquivo já processado, ignorando (arquivo=%s)", nome_arquivo)
        return {"ja_processado": True, **meta}

    db.table("arquivo_agenda_processado").insert({
        "tipo_leiaute": meta["tipo_leiaute"], "ident_ic": meta["ident_ic"],
        "data_req": meta["data_req"], "seq": meta["seq"],
        "iniciado_em": datetime.now(timezone.utc),
    }).execute()

    texto = _abrir_texto(conteudo, meta["comprimido"])
    agrupador = _Agrupador(financiador_id, nome_arquivo, db)
    linhas_lidas = 0
    linhas_sem_agenda = 0
    linhas_rejeitadas_parser = 0

    for numero_linha, campos in enumerate(csv.reader(texto), start=1):
        linhas_lidas += 1
        try:
            resultado = parser_ap005.traduzir_linha(campos, meta["tipo_leiaute"])
        except parser_ap005.LinhaInvalidaError as exc:
            agrupador.finalizar()
            _registrar_rejeitada(db, nome_arquivo, numero_linha, campos, str(exc))
            linhas_rejeitadas_parser += 1
            continue

        if resultado is None:
            agrupador.finalizar()
            linhas_sem_agenda += 1
            continue

        cabecalho, pagamento = resultado
        agrupador.adicionar(numero_linha, campos, cabecalho, pagamento)

    agrupador.finalizar()

    linhas_ok = agrupador.linhas_ok + linhas_sem_agenda
    linhas_rejeitadas = agrupador.linhas_rejeitadas + linhas_rejeitadas_parser

    if linhas_lidas and linhas_rejeitadas / linhas_lidas > _LIMIAR_ALERTA_REJEICAO:
        logger.warning(
            "[ImportarAP005] Taxa de rejeição acima de %.1f%% (arquivo=%s, rejeitadas=%d, lidas=%d)",
            _LIMIAR_ALERTA_REJEICAO * 100, nome_arquivo, linhas_rejeitadas, linhas_lidas,
        )

    (
        db.table("arquivo_agenda_processado")
        .update({
            "linhas_lidas": linhas_lidas, "linhas_ok": linhas_ok, "linhas_rejeitadas": linhas_rejeitadas,
            "concluido_em": datetime.now(timezone.utc),
        })
        .eq("tipo_leiaute", meta["tipo_leiaute"]).eq("ident_ic", meta["ident_ic"])
        .eq("data_req", meta["data_req"]).eq("seq", meta["seq"])
        .execute()
    )

    return {
        "ja_processado": False, "linhas_lidas": linhas_lidas, "linhas_ok": linhas_ok,
        "linhas_rejeitadas": linhas_rejeitadas, **meta,
    }
```

- [ ] **Step 4: Rodar os testes e confirmar que passam** (contra o banco `agenda` real de dev — mesma prática de `test_repository.py`)

Run: `pytest apps/agenda/tests/test_importar_ap005.py -v`
Expected: PASS (6 testes)

- [ ] **Step 5: Commit**

```bash
git add apps/agenda/importar_ap005.py apps/agenda/tests/test_importar_ap005.py
git commit -m "feat: orquestração de ingestão do arquivo AP005 (Plano 08, Task 2)"
```

---

### Task 3: Endpoint HTTP interno (`jobs/importar-ap005/{financiador_id}`)

**Files:**
- Modify: `apps/agenda/views.py`
- Modify: `apps/agenda/urls.py`
- Test: `apps/agenda/tests/test_views_importar_ap005.py`

**Interfaces:**
- Consumes: `apps.agenda.importar_ap005.importar_arquivo`, `apps.agenda.parser_ap005.NomeArquivoInvalidoError` (Task 2); `shared.pubsub_auth.verificar_push_oidc` (Plano 07, já importado em `views.py`).
- Produces: `POST /api/v1/jobs/importar-ap005/{financiador_id}` — `401` sem OIDC válido, `400` sem header `X-Nome-Arquivo` ou nome fora do padrão, `200`/`202` (`ja_processado`) com o dict de `importar_arquivo` serializado em JSON, `500` em falha inesperada.

- [ ] **Step 1: Escrever `apps/agenda/tests/test_views_importar_ap005.py`**

```python
import csv
import io

import pytest
from django.test import Client

from apps.agenda import views
from apps.agenda.parser_ap005 import parse_nome_arquivo
from apps.agenda.repository import _CHAVE_UR, _com_filtros
from shared.cloudsql_client import get_db

FINANCIADOR_TESTE = "12345678000199"
NOME_ARQUIVO = "CERC-AP005_22223333_20260922_0000001_ret.csv"
URL = f"/api/v1/jobs/importar-ap005/{FINANCIADOR_TESTE}"

_CHAVE_TESTE = {
    "data_liquidacao": "2026-09-22",
    "entidade_registradora": "22246686000196",
    "cnpj_credenciadora": "01027058000191",
    "documento_ufr": "99999999000199",
    "documento_titular": "99999999000199",
    "codigo_arranjo": "VCC",
}


def _linha_valida() -> bytes:
    campos = [
        "REF-001", _CHAVE_TESTE["entidade_registradora"], _CHAVE_TESTE["cnpj_credenciadora"],
        _CHAVE_TESTE["documento_ufr"], _CHAVE_TESTE["codigo_arranjo"], _CHAVE_TESTE["data_liquidacao"],
        _CHAVE_TESTE["documento_titular"], "1", "1000.00", "0", "0",
        _CHAVE_TESTE["documento_titular"], "CC", "001", "00000000", "1234", "123456-7",
        "500.00", "", "", "", "", "", "6", "", "", "CTR-VIEW",
        "", "0", "1000.00", "2026-09-22T10:00:00Z",
    ]
    buffer = io.StringIO()
    csv.writer(buffer).writerow(campos)
    return buffer.getvalue().encode("utf-8")


@pytest.fixture(autouse=True)
def _ambiente(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: True)
    db = get_db(FINANCIADOR_TESTE)
    meta = parse_nome_arquivo(NOME_ARQUIVO)

    def _limpar():
        _com_filtros(db.table("agenda_ur_pagamento").delete(), _CHAVE_TESTE, _CHAVE_UR).execute()
        _com_filtros(db.table("agenda_ur").delete(), _CHAVE_TESTE, _CHAVE_UR).execute()
        db.table("arquivo_agenda_processado").delete().eq("tipo_leiaute", meta["tipo_leiaute"]).eq(
            "ident_ic", meta["ident_ic"]).eq("data_req", meta["data_req"]).eq("seq", meta["seq"]).execute()

    _limpar()
    yield
    _limpar()


def test_sem_oidc_retorna_401(monkeypatch):
    monkeypatch.setattr(views, "verificar_push_oidc", lambda request: False)
    response = Client().post(URL, data=_linha_valida(), content_type="application/octet-stream")
    assert response.status_code == 401


def test_sem_header_nome_arquivo_retorna_400():
    response = Client().post(URL, data=_linha_valida(), content_type="application/octet-stream")
    assert response.status_code == 400


def test_nome_arquivo_invalido_retorna_400():
    response = Client().post(
        URL, data=_linha_valida(), content_type="application/octet-stream",
        HTTP_X_NOME_ARQUIVO="arquivo_qualquer.csv",
    )
    assert response.status_code == 400


def test_importa_arquivo_com_sucesso():
    response = Client().post(
        URL, data=_linha_valida(), content_type="application/octet-stream",
        HTTP_X_NOME_ARQUIVO=NOME_ARQUIVO,
    )
    assert response.status_code == 200
    corpo = response.json()
    assert corpo["linhas_ok"] == 1
    assert corpo["ja_processado"] is False

    db = get_db(FINANCIADOR_TESTE)
    ur = _com_filtros(db.table("agenda_ur").select("*"), _CHAVE_TESTE, _CHAVE_UR).execute().data
    assert len(ur) == 1
```

- [ ] **Step 2: Rodar os testes e confirmar que falham**

Run: `pytest apps/agenda/tests/test_views_importar_ap005.py -v`
Expected: FAIL com `AttributeError: module 'apps.agenda.views' has no attribute 'importar_ap005'` (ou 404, já que a rota ainda não existe)

- [ ] **Step 3: Adicionar o endpoint em `apps/agenda/views.py`**

No topo do arquivo, junto aos outros imports de `apps.agenda`:

```python
from apps.agenda import parser_ap005
from apps.agenda.importar_ap005 import importar_arquivo
```

No final do arquivo (depois de `varrer_completude`):

```python
@require_POST
def importar_ap005(request, financiador_id: str):
    """Endpoint interno de ingestão de arquivo AP005/AP005A/AP005B (design
    doc §9/§14 item 4). A conexão real (SFTP/Bucket/Connect:Direct) é uma
    dependência de infra ainda a definir — decisão do Plano 08 (Global
    Constraints): por ora, o arquivo chega aqui já em mãos (nome no header
    X-Nome-Arquivo, conteúdo no corpo bruto da requisição, lido via stream
    — nunca request.body, que materializaria tudo em memória), protegido
    pelo mesmo OIDC dos outros jobs internos. Um plano futuro liga isso a
    Cloud Scheduler + o canal real, quando a credencial existir."""
    if not verificar_push_oidc(request):
        return JsonResponse({"erro": "OIDC inválido"}, status=401)

    nome_arquivo = request.META.get("HTTP_X_NOME_ARQUIVO")
    if not nome_arquivo:
        return JsonResponse({"erro": "header X-Nome-Arquivo é obrigatório"}, status=400)

    try:
        resultado = importar_arquivo(financiador_id, nome_arquivo, request)
    except parser_ap005.NomeArquivoInvalidoError as exc:
        return JsonResponse({"erro": str(exc)}, status=400)
    except Exception:
        logger.exception(
            "[ImportarAP005] Falha ao importar arquivo (financiador=%s, arquivo=%s)", financiador_id, nome_arquivo,
        )
        return JsonResponse({"erro": "falha ao importar arquivo"}, status=500)

    status = 202 if resultado.get("ja_processado") else 200
    return JsonResponse(resultado, status=status)
```

- [ ] **Step 4: Registrar a rota em `apps/agenda/urls.py`**

```python
from django.urls import path, re_path
from . import views

urlpatterns = [
    path("health", views.health),
    re_path(r"^webhooks/agenda/(?P<financiador_id>\d{14})$", views.webhook_agenda),
    path("webhooks/agenda/processar", views.processar_webhook_agenda),
    path("jobs/varrer-completude", views.varrer_completude),
    re_path(r"^jobs/importar-ap005/(?P<financiador_id>\d{14})$", views.importar_ap005),
]
```

- [ ] **Step 5: Rodar os testes e confirmar que passam**

Run: `pytest apps/agenda/tests/test_views_importar_ap005.py -v`
Expected: PASS (4 testes)

- [ ] **Step 6: Rodar a suíte inteira do app para checar regressão**

Run: `pytest apps/agenda -v`
Expected: PASS em todos os testes (Planos 01-08)

- [ ] **Step 7: Commit**

```bash
git add apps/agenda/views.py apps/agenda/urls.py apps/agenda/tests/test_views_importar_ap005.py
git commit -m "feat: endpoint interno de ingestão de arquivo AP005 (Plano 08, Task 3)"
```
