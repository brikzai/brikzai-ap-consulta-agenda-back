# SPEC 03 — Serviço de Consulta de Agenda de Recebíveis (CERC-AP005)

> **Status:** pronta para implementação
> **Público-alvo:** agente de código / squad de engenharia
> **Papel na cadeia CERC:** Financiador (IF ou Não Financeira)
> **Canais:** API REST síncrona + Webhook assíncrono (`tipoEvento = agenda`) + Arquivo CSV (AP005 / AP005A / AP005B)
> **Versão da API CERC:** 1.5 (`v15`)
> **Fonte normativa:** docs.cerc.com — Financiador › Consulta de agenda › Agenda
> **Base regulatória:** processo 5.7 da convenção associada à Circular BCB nº 3.952/2019, com atualizações BCB nº 264 e CMN nº 5.045/2022

> **Nota de escopo:** esta spec substitui e expande as seções §4.3 e §5.5 da **SPEC 01**. A partir de agora, consulta de agenda é um serviço próprio (`agenda-service`); a SPEC 01 mantém apenas opt-in/opt-out.

---

## 0. As três regras que governam este serviço

1. **Agenda ≠ contrato.** Uma agenda é o conjunto de URs que compartilham **o mesmo Estabelecimento Comercial + Arranjo de Pagamento + Credenciadora**. Cada combinação dessas três dimensões é uma agenda distinta. O volume de agendas de um EC é proporcional ao número de credenciadoras com que ele opera × arranjos autorizados.
2. **Frescor tem corte às 6h.** Batch e online entregam recebíveis atualizados **até as 6h do dia da disponibilização**. A consulta **online** difere por trazer dados de **contratos** atualizados até o instante da consulta. Nenhum dos dois entrega recebíveis em tempo real.
3. **Consulta online é opt-in instantâneo.** A própria ação de consultar configura autorização de acesso e **está sujeita a fiscalização do regulador**. Toda consulta online precisa de trilha de auditoria com base autorizativa identificável (§8). Este é o requisito de compliance mais importante da spec.

---

## 1. Escopo

| Capacidade | Interface CERC |
|---|---|
| Consulta de agenda **batch** (síncrona) | `POST /v15/agenda/consultar` (sem `online` ou `online=false`) |
| Consulta de agenda **online** (síncrona + assíncrona) | `POST /v15/agenda/consultar?online=true` |
| Recebimento assíncrono de URs da interoperabilidade | Webhook `tipoEvento = agenda` |
| Ingestão de agenda batch por arquivo | AP005 / AP005A / AP005B (`*.csv`, `*.csv.gz`) via SFTP/Bucket/Connect:Direct |
| Avaliação com indicadores de consistência | `tipoAvaliacao` na requisição |

**Fora do escopo:** opt-in/opt-out (SPEC 01); registro de contratos e efeitos (SPEC 02); Radar de Recebíveis e Consulta de Relações (produtos separados).

**Pré-condição funcional:** só existe agenda acessível onde existe **opt-in válido** — por força de opt-in (SPEC 01) ou por força de contrato (SPEC 02). Ausência de base autorizativa retorna `105802`.

---

## 2. Modos de acesso

| | **Batch (arquivo)** | **Batch (API)** | **Online (API)** |
|---|---|---|---|
| Disponibilidade | AP005 diário via SFTP | `POST /v15/agenda/consultar` | `POST /v15/agenda/consultar?online=true` |
| Frescor de recebíveis | até 6h do dia | até 6h do dia | até 6h do dia |
| Frescor de contratos | até 6h do dia | até 6h do dia | **até o momento da consulta** |
| Interoperabilidade | incluída no arquivo | último snapshot conhecido | consulta **ativa** a cada registradora |
| Retorno | arquivo CSV | `200` síncrono | `200` síncrono **+ webhook por UR** |
| Opt-in instantâneo | não | não | **sim** (§0.3) |

> A consulta **online está disponível exclusivamente via API**. O canal de arquivo suporta **apenas** agenda batch.

**Regra de escolha (implementar como política, não deixar a critério do chamador):**

- Análise de crédito / decisão de oneração → **online**.
- Monitoramento de carteira, dashboards, reprocessamento em massa → **batch** (arquivo, se disponível; senão API batch).
- Nunca usar online para varredura em massa: além do custo, cada consulta é um opt-in instantâneo fiscalizável.

---

## 3. Autenticação

Idêntica à SPEC 01 §3 (OAuth 2.0 client credentials, `api.int.cerc.com` / `api.prd.cerc.com`). **Reutilizar** o mesmo `TokenProvider` das SPECs 01/02.

---

## 4. `POST /v15/agenda/consultar`

### 4.1 Query param

| Param | Tipo | Default | Efeito |
|---|---|---|---|
| `online` | boolean | `false` | `false` → batch: último dado conhecido de cada agenda, tudo no response. `true` → dispara pedido online a cada registradora envolvida; URs da CERC voltam no response, URs da **interoperabilidade** chegam por **webhook** |

### 4.2 Request

```jsonc
{
  "listaCnpjCredenciadora": ["99T"],                  // [obrigatório] ou lista de CNPJs
  "documentoUsuarioFinalRecebedor": "22751826000125", // [obrigatório] CPF 11 / CNPJ 14, zero-pad
  "documentoTitular": "22751826000125",               // opcional
  "listaCodigoArranjoPagamento": ["99T"],             // [obrigatório] ou lista de códigos
  "dataInicio": "2026-08-17",                         // [obrigatório] AAAA-MM-DD
  "dataFim": "2026-11-17",                            // [obrigatório] >= dataInicio
  "tipoAvaliacao": "avaliacao_agenda_basica_ap",      // opcional
  "participante": "12345678000199",                   // opcional — CNPJ do agente de registro
                                                      // responsável pelo faturamento (admin legal da carteira)
  "carteira": "CARTEIRA-01"                           // opcional; obrigatório p/ "Prestador de Serviço"
}
```

- Campos obrigatórios pelo schema: `listaCnpjCredenciadora`, `documentoUsuarioFinalRecebedor`, `listaCodigoArranjoPagamento`, `dataInicio`, `dataFim`.
- `99T` = curinga (todas as credenciadoras / todos os arranjos). **Não misturar** `99T` com valores específicos na mesma lista.
- `tipoAvaliacao` nesta rota aceita **apenas**: `avaliacao_agenda_basica_ap`, `avaliacao_agenda_completa_ap`. Os valores `avaliacao_contrato_*` pertencem às rotas de contrato (SPEC 02) — rejeitar localmente.

### 4.3 Response `200` — estrutura

Array de agendas. Cada item:

```jsonc
{
  "entidadeRegistradora": "23399607000191",
  "instituicaoCredenciadora": "36216798000150",
  "codigoArranjoPagamento": "VCC",
  "documentoUsuarioFinalRecebedor": "22751826000125",
  "indicadoresConsistencia": [ /* ver §4.5 */ ],
  "unidadesRecebiveis": [
    {
      "dataLiquidacao": "2026-09-04",
      "constituicao": "1",                       // 1 = Constituída, 2 = A constituir (fumaça)
      "valorConstituidoTotal": 1000.00,
      "valorConstituidoAntecipacaoPre": 0.00,
      "valorBloqueado": 0.00,
      "valorLivre": 1000.00,
      "valorTotalUR": 1000.00,
      "dataHoraUltimaAtualizacao": "2026-08-17T04:58:36.087Z",
      "pagamentos": [ /* ver §4.4 */ ],
      "titulares": [                             // fração da UR por titular
        {
          "documentoTitular": "22751826000125",
          "valorConstituidoTotal": 1000.00,
          "valorConstituidoAntecipacaoPre": 0.00,
          "valorBloqueado": 0.00,
          "valorLivre": 1000.00,
          "dataHoraUltimaAtualizacao": "2026-08-17T04:58:36.087Z",
          "pagamentos": [ /* mesma estrutura */ ]
        }
      ]
    }
  ]
}
```

### 4.4 `pagamentos[]` — semântica

| Campo | Regra |
|---|---|
| `DomicilioPagamento` | obrigatório; ver §4.6 |
| `valorAPagar` | ≥ 0; valor previsto a ser pago pela credenciadora |
| `beneficiario` | CNPJ do beneficiário em decorrência do contrato, para efeito de ônus |
| `dataLiquidacaoEfetiva` / `valorLiquidacaoEfetiva` | preenchidos **apenas após a liquidação** do recebível |
| `motivoDeNaoPagamento` | `001` dados bancários inválidos · `002` liquidação bloqueada · `999` outros |
| `tipoInformacaoPagamento` | `1` troca de titularidade · `2` ônus cessão fiduciária · `3` ônus outros · `4` bloqueio judicial · `5` antecipação pós-contratada · `6` **liquidação** · `7` **domicílio de pagamento** *(o arquivo AP005 acrescenta `8` promessa de cessão — §6.3)* |
| `regrasDivisao` / `valorOnerado` | preenchidos quando a informação é de efeito de contrato |
| `indicadorEfeitosContrato` | sequencial do efeito sobre a UR (coexistência de múltiplos ônus) — obrigatório quando é efeito de contrato |
| `valorConstituidoEfeito` | ≥ 0; montante do constituído total afetado pelo efeito — obrigatório quando é efeito de contrato |

> **`tipoInformacaoPagamento` mistura três naturezas** na mesma lista: efeitos de contrato (1–4), antecipação (5), liquidação (6) e domicílio (7). O parser **deve** ramificar por esse campo antes de interpretar os demais — tratar tudo como efeito de contrato produz valores errados.

### 4.5 Regras de cálculo (críticas)

- **`valorTotalUR` é a base de cálculo dos efeitos de contrato.** Equivale à soma dos valores de **todas as frações de UR do mesmo usuário final recebedor, independentemente do titular**. Nunca usar `valorConstituidoTotal` de um titular como base de oneração.
- `valorConstituidoTotal` (nível UR) é o total líquido a pagar pela credenciadora para o titular indicado.
- `valorLivre` é o disponível para nova oneração; é o número que alimenta decisões de crédito.
- `valorBloqueado` **não compõe** o valor constituído total.
- `valorConstituidoAntecipacaoPre` é a parcela oriunda de **antecipação pré-contratada** — não somar duas vezes com o constituído total.
- `constituicao = 2` ("a constituir" / **fumaça**) representa expectativa futura, não direito constituído. Segregar em toda visão de crédito e nunca somar com constituídas sem rótulo explícito.

### 4.6 `DomicilioPagamento`

`numeroDocumentoTitular` (obrig.) · `nomeTitular` · `tipoConta` (obrig.) · `compe` (3 dígitos, opcional) · `ispb` (8 dígitos, obrig.) · `agencia` (obrig. se `tipoConta ≠ PG`, sem DV) · `numeroConta` (obrig.).

**Domínio de `tipoConta` — divergente por canal (implementar como união):**

| Código | Descrição | API contratos (SPEC 02) | Webhook/arquivo agenda |
|---|---|:--:|:--:|
| `CC` | Conta corrente | ✔ | ✔ |
| `CD` | Conta de depósito | ✔ | ✔ |
| `PG` | Conta de pagamento | ✔ | ✔ |
| `PP` | Conta poupança | ✔ | ✔ |
| `CG` | Conta garantia | — | ✔ |
| `CI` | Conta de investimento | — | ✔ |

`numeroConta`: contas `CC`/`CD`/`CG`/`CI`/`PP` → número **com DV separado por hífen** (`999999-9`; se o DV for alfanumérico, substituir por zero). Conta `PG` → **sem hífen**.

---

## 5. Webhook `tipoEvento = agenda`

### 5.1 Comportamento

- Disparado a cada consulta **online** que envolva URs da interoperabilidade.
- **Uma UR por requisição** — não há lote e **não há sinal de fim**.
- URs registradas na CERC voltam no response síncrono.
- URs de **outras registradoras** podem vir **também** no response síncrono, desde que já tenham sido previamente solicitadas por qualquer financiador na borda CERC (via arquivo batch ou consulta online). Mesmo assim, **o status mais atualizado sempre chega via webhook** — o webhook prevalece sobre o síncrono em caso de divergência.

### 5.2 Payload

Envelope padrão `{ tipoEvento, dataHoraEvento, evento }`. O `evento` traz, por UR: `entidadeRegistradora`, `instituicaoCredenciadora`, `documentoUsuarioFinalRecebedor`, `codigoArranjoPagamento`, `documentoTitular`, `dataLiquidacao`, `constituicao`, `valorConstituidoTotal`, `valorConstituidoAntecipacaoPre`, `valorBloqueado`, `valorLivre`, `valorTotalUR`, `dataHoraUltimaAtualizacao` (RFC3339), `carteira` (opcional), `pagamentos[]` (com `domicilioPagamentos`), `erros[]` (obrigatório em caso de erro).

### 5.3 Requisitos do receptor

Idênticos à SPEC 01 §4.4: OAuth2 **ou** Basic, resposta **2xx**, **até 5 tentativas e nada além disso**, **500 req/s** mínimo, gravação em `webhook_inbox` **antes** de processar, resposta em < 200 ms, deduplicação por hash.

> Consulta online de um EC grande pode gerar **milhares de requisições** de webhook em rajada (uma por UR). Dimensionar o receptor para rajada, não para média.

### 5.4 Correlação consulta ↔ webhook (lacuna a resolver)

O payload do webhook **não carrega** um identificador da consulta que o originou. A correlação precisa ser reconstruída pela chave de negócio:

```
(instituicaoCredenciadora, codigoArranjoPagamento, documentoUsuarioFinalRecebedor,
 documentoTitular, dataLiquidacao)
```

**Algoritmo:** manter índice de consultas online "em janela" (`AGUARDANDO_WEBHOOK`); ao receber uma UR, casar contra as consultas cujos filtros a contêm (tratando `99T` como universo e a janela `dataInicio..dataFim` como intervalo). Se casar com mais de uma, **anexar a todas**. Se não casar com nenhuma, gravar em `agenda_ur_orfa` — nunca descartar; é dado autorizado e válido.

### 5.5 Critério de completude

Como não há sinal de fim:

- Primeira UR recebida → consulta em `PARCIAL`.
- **Quiet period** configurável (default **90 s**) sem novas URs para a consulta → `COMPLETA`.
- **Hard timeout** de **15 min** → `COMPLETA_COM_TIMEOUT` + alerta.
- Sempre expor `dataHoraUltimaAtualizacao` por UR para o consumidor decidir frescor.
- Nunca bloquear a resposta ao usuário esperando o webhook: entregar o síncrono imediatamente e enriquecer depois.

### 5.6 Teste de webhook em homologação

Em homologação não há consultas reais à Interop, o que historicamente impedia testar o fluxo assíncrono. A CERC habilitou o disparo de webhook **mesmo sem consulta à Interop**, desde que o EC consultado esteja registrado em uma destas credenciadoras:

| Credenciadora (CNPJ) |
|---|
| `72253725000100` |
| `09700540000152` |
| `49240077000128` |

ECs de exemplo cadastrados nessas credenciadoras: `07027955000181`, `15076963000146`, `98990252000100`.

**Usar esses CNPJs nos testes E2E de homologação (§10.2).** Sem eles, o caminho do webhook simplesmente não é exercitado e a integração vai para produção sem QA do fluxo assíncrono.

---

## 6. Canal de arquivo — AP005

### 6.1 Dados gerais

Emissor: CERC · Destinatário: Participante · Transmissão: Portal CERC ou **SFTP / Bucket / Connect:Direct** · Formato: CSV · Periodicidade: **sob demanda ou durante a vigência de contrato ou opt-in** · Arquivo de retorno: N/D.

**Nomenclatura:** `{Tipo_Leiaute}_{Ident_IC}_{DataReq}_{Seq}_ret.csv`
`Tipo_Leiaute` fixo `CERC-AP005` · `Ident_IC` = **raiz do CNPJ** (8 dígitos) · `DataReq` = `YYYYMMDD` · `Seq` = 7 dígitos a partir de `0000001`.
Exemplo: `CERC-AP005_53462828_20190221_0000001_ret.csv`
**Diretório:** `/informacoes_agendas/saida/`

### 6.2 Layout do arquivo de remessa

| Col. | Campo | Formato | Regra |
|---|---|---|---|
| 1 | Referência externa | Alfa | Obrigatório — código de controle do solicitante indicado **no opt-in ou no contrato** |
| 2 | Entidade registradora | Alfa | Obrigatório — CNPJ 14, zero-pad |
| 3 | Instituição credenciadora/subcredenciadora | Alfa | Obrigatório — CNPJ 14, zero-pad |
| 4 | Usuário final recebedor | Alfa | Obrigatório — CPF 11 / CNPJ 14, zero-pad |
| 5 | Arranjo de pagamento | Alfa | Obrigatório — domínio vigente |
| 6 | Data de liquidação | Data | Obrigatório — `AAAA-MM-DD` |
| 7 | Titular da UR | Alfa | Obrigatório — CPF 11 / CNPJ 14 |
| 8 | Constituição da UR | Alfa | Obrigatório — `1` constituída · `2` a constituir |
| 9 | Valor constituído total | Decimal | Obrigatório |
| 10 | Valor constituído antecipação pré-contratada | Decimal | Obrigatório |
| 11 | Valor bloqueado | Decimal | Obrigatório |
| **12** | **Lista de informações de pagamento** | — | Opcional em caso de **baixa de UR sem pagamentos a fazer** |
| 12.1 | Número documento titular domicílio | Alfa | Obrigatório |
| 12.2 | Tipo de conta | Alfa | Obrigatório e atualizável — `CC`, `CD`, `CG`, `CI`, `PG`, `PP` |
| 12.3 | COMPE | Alfa(3) | Opcional |
| 12.4 | ISPB | Alfa(8) | Obrigatório |
| 12.5 | Agência | Alfa | Obrigatório se tipo de conta ≠ `PG`; sem DV |
| 12.6 | Número da conta / conta de pagamento | Alfa(20) | Obrigatório — com DV e hífen (`CC`,`CD`,`CG`,`CI`,`PP`); sem hífen (`PG`) |
| 12.7 | Valor a pagar | Decimal | Obrigatório — informado pela credenciadora no registro |
| 12.8 | Beneficiário | Alfa | Opcional — CNPJ do beneficiário, para efeito de ônus |
| 12.9 | Data de liquidação efetiva | Data | Obrigatório **se a informação for de liquidação** |
| 12.10 | Valor de liquidação efetiva | Decimal | Obrigatório **se a informação for de liquidação** |
| 12.11 | Regra de divisão | Alfa | Obrigatório **se efeito de contrato** — `1` valor definido · `2` percentual |
| 12.12 | Valor onerado na UR | Decimal | Obrigatório **se efeito de contrato** |
| 12.13 | Tipo de informação de pagamento | Alfa | Obrigatório — `1`–`8` (§6.3) |
| 12.14 | Indicador de ordem do efeito | Alfa | Obrigatório **se efeito de contrato** |
| 12.15 | Valor constituído do efeito de contrato na UR | Decimal | Obrigatório **se efeito de contrato** |
| **12.16** | **Identificador CERC do contrato** | Alfa | Obrigatório **se efeito de contrato** — ver §6.4 |
| 13 | Carteira | Alfa | Opcional |
| 14 | Valor livre | Decimal | Obrigatório |
| 15 | Valor total da UR | Decimal | Obrigatório — base de cálculo dos efeitos (§4.5) |
| 16 | Data/hora da última atualização da UR | Alfa | Obrigatório — **RFC3339** |

**Arquivo sem informações de agenda** — layout reduzido: 1 `Referência externa` · 2 `Data/hora do processamento` (RFC3339) · 3 `Lista de erros` (3.1 código numérico, 3.2 descrição, máx. 1000 caracteres, obrigatória se status = 1).
O parser **deve** detectar esse layout pela contagem de colunas e tratá-lo como resultado válido "sem agenda", nunca como arquivo corrompido.

### 6.3 `tipoInformacaoPagamento` (col. 12.13) — domínio completo do arquivo

`1` troca de titularidade · `2` ônus cessão fiduciária · `3` ônus outros · `4` bloqueio judicial · `5` antecipação pós-contratada · `6` liquidação · `7` domicílio de pagamento · **`8` promessa de cessão**.

> O valor `8` existe no arquivo e no OpenAPI do webhook de agenda **não** aparece na enumeração publicada (que vai até `7`). Mesma inconsistência apontada na SPEC 02 §12.1 para `tipoEfeito`. **Decisão:** o parser aceita `1`–`8` em ambos os canais; qualquer valor fora disso vira `agenda_ur_rejeitada` com alerta, nunca exceção não tratada.

### 6.4 Campo 12.16 — Identificador CERC do contrato

Campo adicionado ao leiaute: disponível em **homologação desde 01/08/2025** e **obrigatório em produção desde 03/11/2025**. Entre essas datas a adoção antecipada em produção era opcional, mediante solicitação no portal do cliente.

**Valor prático:** é o que permite ligar cada linha de efeito de pagamento ao **contrato que a originou** — sem ele, a conciliação agenda × contrato depende de heurística. Persistir e indexar; usar como chave de junção com `contrato.id_contrato_cerc` (SPEC 02).

**Implementação defensiva:** o parser deve aceitar arquivos **com e sem** a coluna (detecção por contagem de colunas), já que ambientes antigos e reprocessamentos históricos podem não tê-la.

### 6.5 Segregação de arquivos (serviços opcionais)

Por padrão, tudo vem em um único AP005. Habilitando os serviços (via portal do cliente), o participante recebe arquivos separados:

| Arquivo | Conteúdo |
|---|---|
| `CERC-AP005` | agendas por força de **opt-in** / URs **constituídas** (conforme serviços ativos) |
| `CERC-AP005A` | agendas por força de **contrato** |
| `CERC-AP005B` | URs **não constituídas (fumaça)**, independente do tipo de opt-in |

**Cenários:**

| Serviços habilitados | Arquivos recebidos |
|---|---|
| Nenhum (padrão) | AP005 (todos os tipos) |
| Só separação por tipo de opt-in | AP005 + AP005A |
| Só separação por constituição | AP005 + AP005B |
| Ambos | AP005 (constituídas por opt-in) + AP005A (constituídas por contrato) + AP005B (fumaça de ambos) |

**Regras de implementação:**

- O **layout é idêntico** nos três — muda apenas nomenclatura e separação. Um único parser atende aos três; a origem é derivada do nome do arquivo, não do conteúdo.
- **AP005A e AP005B são entregues compactados em `.gzip`** (`.csv.gz`). O ingestor deve descompactar por streaming, sem materializar o arquivo inteiro em memória.
- Ganho operacional da separação por tipo: processar **primeiro** as agendas por força de opt-in, acelerando ofertas de crédito. Implementar o pipeline com prioridade por origem.
- Recomendação: habilitar **ambos** os serviços. Fumaça (AP005B) tende a ser o maior volume e o menor valor de decisão — isolá-la evita degradar o processamento das constituídas.

### 6.6 Requisitos do ingestor

- Idempotência por `(tipo_leiaute, ident_ic, data_req, seq)`; reprocessar o mesmo arquivo não pode duplicar URs.
- Processar em streaming (arquivos de EC grande passam de milhões de linhas).
- Chave natural da UR: `(entidade_registradora, credenciadora, ufr, titular, arranjo, data_liquidacao)`.
- Linhas rejeitadas vão para `agenda_ur_rejeitada` com o motivo; **nunca** abortar o arquivo inteiro por uma linha inválida — registrar, contabilizar e seguir.
- Alertar quando a taxa de rejeição exceder 0,5 % das linhas.

---

## 7. API interna do serviço

Base `/api/v1`. Auth: Bearer JWT interno.

### 7.1 `POST /api/v1/agendas/consultas`

```jsonc
{
  "modo": "ONLINE" | "BATCH",
  "usuarioFinalRecebedor": "22751826000125",
  "titular": "22751826000125",
  "credenciadoras": ["99T"],
  "arranjos": ["99T"],
  "dataInicio": "2026-08-17",
  "dataFim": "2026-11-17",
  "tipoAvaliacao": "avaliacao_agenda_basica_ap",
  "baseAutorizativa": { "tipo": "OPTIN" | "CONTRATO", "id": "opt_01J8ZK..." },  // [obrigatório]
  "motivo": "ANALISE_CREDITO"                                                   // [obrigatório] p/ auditoria
}
```

- `BATCH` → `200` com o consolidado.
- `ONLINE` → `202` `{ "consultaId": "...", "status": "PARCIAL", "agendas": [...] }` — já devolve o que veio no síncrono; o webhook enriquece depois.
- `baseAutorizativa` é **obrigatória** e validada contra a SPEC 01 antes da chamada: opt-in `ATIVO` cobrindo o UFR, as credenciadoras, os arranjos e a janela. Sem cobertura → `403 SEM_BASE_AUTORIZATIVA`, sem chamar a CERC (§0.3, evita `105802`).

### 7.2 `GET /api/v1/agendas/consultas/{consultaId}`

Retorna o consolidado com `status` (`PARCIAL` | `COMPLETA` | `COMPLETA_COM_TIMEOUT`), contagem de URs por origem (`SINCRONO` | `WEBHOOK` | `ARQUIVO`) e `dataHoraUltimaAtualizacao` mais antiga e mais recente do conjunto.

### 7.3 `GET /api/v1/agendas/urs`

Consulta o **repositório consolidado** (não chama a CERC). Filtros: `ufr`, `titular`, `credenciadora`, `arranjo`, `dataLiquidacaoInicio/Fim`, `constituicao`, `origem`, `atualizadoDesde`. Paginação por cursor, `limit` ≤ 1000.

### 7.4 `GET /api/v1/agendas/urs/posicao`

Visão agregada para decisão de crédito, por UFR e janela:

```jsonc
{
  "valorTotalConstituido": 0.00,
  "valorLivre": 0.00,          // base para nova oneração
  "valorBloqueado": 0.00,
  "valorOnerado": 0.00,
  "valorFumaca": 0.00,         // constituicao = 2, SEMPRE segregado
  "porCredenciadora": [ /* … */ ],
  "porArranjo": [ /* … */ ],
  "frescor": { "maisAntigo": "…", "maisRecente": "…" }
}
```

**Regra de apresentação:** `valorFumaca` nunca é somado aos demais totais sem rótulo explícito.

---

## 8. Compliance da consulta online (requisito regulatório)

Consulta online = opt-in instantâneo, **passível de fiscalização**. Obrigatório:

1. **Base autorizativa registrada em toda consulta** — `baseAutorizativa` + `motivo`, persistidos em `consulta_agenda`.
2. **Ator identificado** — usuário/sistema que originou, com timestamp e IP/serviço.
3. **Retenção de 5 anos** da trilha (alinhado à SPEC 01 §8).
4. **Bloqueio de varredura** — rate limit por UFR (default: **10 consultas online / UFR / dia**, configurável) e alerta em consultas online sem `baseAutorizativa` de opt-in ativo.
5. **Relatório de consultas** exportável por período, UFR e ator, pronto para atender demanda do regulador.

---

## 9. Modelo de dados

```sql
CREATE TABLE consulta_agenda (
  id                  TEXT PRIMARY KEY,
  modo                TEXT NOT NULL,               -- ONLINE | BATCH
  status              TEXT NOT NULL,               -- PARCIAL|COMPLETA|COMPLETA_COM_TIMEOUT|ERRO
  filtro_ufr          TEXT NOT NULL,
  filtro_titular      TEXT,
  filtro_credenciadoras TEXT[] NOT NULL,
  filtro_arranjos     TEXT[] NOT NULL,
  filtro_data_inicio  DATE NOT NULL,
  filtro_data_fim     DATE NOT NULL,
  tipo_avaliacao      TEXT,
  carteira            TEXT,
  base_autorizativa_tipo TEXT NOT NULL,            -- OPTIN | CONTRATO
  base_autorizativa_id   TEXT NOT NULL,
  motivo              TEXT NOT NULL,
  ator                TEXT NOT NULL,
  qtd_urs_sincrono    INT DEFAULT 0,
  qtd_urs_webhook     INT DEFAULT 0,
  iniciada_em         TIMESTAMPTZ NOT NULL,
  ultima_ur_em        TIMESTAMPTZ,
  encerrada_em        TIMESTAMPTZ
);
CREATE INDEX ON consulta_agenda (filtro_ufr, iniciada_em);
CREATE INDEX ON consulta_agenda (status) WHERE status = 'PARCIAL';

-- Repositório consolidado de URs (upsert por chave natural)
CREATE TABLE agenda_ur (
  entidade_registradora  TEXT NOT NULL,
  cnpj_credenciadora     TEXT NOT NULL,
  documento_ufr          TEXT NOT NULL,
  documento_titular      TEXT NOT NULL,
  codigo_arranjo         TEXT NOT NULL,
  data_liquidacao        DATE NOT NULL,
  constituicao           TEXT NOT NULL,            -- 1 constituída | 2 fumaça
  valor_constituido_total            NUMERIC(18,2) NOT NULL,
  valor_constituido_antecipacao_pre  NUMERIC(18,2) NOT NULL DEFAULT 0,
  valor_bloqueado        NUMERIC(18,2) NOT NULL DEFAULT 0,
  valor_livre            NUMERIC(18,2) NOT NULL DEFAULT 0,
  valor_total_ur         NUMERIC(18,2) NOT NULL,   -- base de cálculo dos efeitos
  carteira               TEXT,
  data_hora_ultima_atualizacao TIMESTAMPTZ NOT NULL,
  origem                 TEXT NOT NULL,            -- SINCRONO | WEBHOOK | ARQUIVO
  origem_arquivo         TEXT,                     -- AP005 | AP005A | AP005B
  atualizado_em          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (entidade_registradora, cnpj_credenciadora, documento_ufr,
               documento_titular, codigo_arranjo, data_liquidacao)
);
CREATE INDEX ON agenda_ur (documento_ufr, data_liquidacao);
CREATE INDEX ON agenda_ur (constituicao, data_liquidacao);

-- Regra de upsert: só sobrescrever quando
--   EXCLUDED.data_hora_ultima_atualizacao >= agenda_ur.data_hora_ultima_atualizacao
-- Empate: WEBHOOK vence SINCRONO, que vence ARQUIVO (§5.1).

CREATE TABLE agenda_ur_pagamento (
  entidade_registradora TEXT, cnpj_credenciadora TEXT, documento_ufr TEXT,
  documento_titular TEXT, codigo_arranjo TEXT, data_liquidacao DATE,
  tipo_informacao_pagamento TEXT NOT NULL,          -- 1..8
  indicador_efeitos_contrato TEXT,
  identificador_cerc_contrato TEXT,                 -- col. 12.16 → junção com SPEC 02
  regras_divisao TEXT, valor_onerado NUMERIC(18,2),
  valor_constituido_efeito NUMERIC(18,2), valor_a_pagar NUMERIC(18,2),
  beneficiario TEXT,
  data_liquidacao_efetiva DATE, valor_liquidacao_efetiva NUMERIC(18,2),
  motivo_nao_pagamento TEXT,                        -- 001 | 002 | 999
  domicilio JSONB NOT NULL,
  PRIMARY KEY (entidade_registradora, cnpj_credenciadora, documento_ufr,
               documento_titular, codigo_arranjo, data_liquidacao,
               tipo_informacao_pagamento, COALESCE(indicador_efeitos_contrato,''))
);
CREATE INDEX ON agenda_ur_pagamento (identificador_cerc_contrato);

CREATE TABLE consulta_agenda_ur (                   -- vínculo N:N consulta ↔ UR
  consulta_id TEXT REFERENCES consulta_agenda(id),
  entidade_registradora TEXT, cnpj_credenciadora TEXT, documento_ufr TEXT,
  documento_titular TEXT, codigo_arranjo TEXT, data_liquidacao DATE,
  origem TEXT NOT NULL, recebida_em TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (consulta_id, entidade_registradora, cnpj_credenciadora,
               documento_ufr, documento_titular, codigo_arranjo, data_liquidacao));

CREATE TABLE agenda_ur_orfa (                        -- webhook sem consulta correlacionável
  id BIGSERIAL PRIMARY KEY, payload JSONB NOT NULL, recebida_em TIMESTAMPTZ NOT NULL);

CREATE TABLE agenda_ur_rejeitada (
  id BIGSERIAL PRIMARY KEY, origem TEXT NOT NULL, arquivo TEXT,
  linha INT, conteudo TEXT, motivo TEXT NOT NULL, ocorrida_em TIMESTAMPTZ NOT NULL);

CREATE TABLE arquivo_agenda_processado (
  tipo_leiaute TEXT, ident_ic TEXT, data_req DATE, seq INT,
  linhas_lidas INT, linhas_ok INT, linhas_rejeitadas INT,
  iniciado_em TIMESTAMPTZ, concluido_em TIMESTAMPTZ,
  PRIMARY KEY (tipo_leiaute, ident_ic, data_req, seq));

CREATE TABLE indicador_consistencia_agenda (
  consulta_id TEXT REFERENCES consulta_agenda(id),
  indicador TEXT, resultado TEXT, parametros JSONB, criticidade TEXT,
  PRIMARY KEY (consulta_id, indicador));
```

`NUMERIC(18,2)` no banco, `BigDecimal`/`decimal` na aplicação. **Proibido `float`/`double`.**

---

## 10. Catálogo de erros AP005 (prefixo 105)

| Código | Descrição | Tratamento |
|---|---|---|
| 105001 | Não foram encontrados registros com os filtros do opt-in/contrato | **Não é erro** — resultado vazio legítimo. Retornar `200` com lista vazia |
| 105002 | UFR ou titular não encontrados | `422` |
| 105003 | Falha na comunicação com a entidade registradora responsável | **Retentável** com backoff; resultado pode vir parcial |
| 105004 / 105005 | Credenciadora obrigatória / CPF-CNPJ inválido | `422` — barrar localmente |
| 105006 / 105007 | UFR obrigatório / inválido | `422` — barrar localmente |
| 105008 / 105009 | Arranjo obrigatório / inválido | `422`; disparar sync da tabela de domínio |
| 105010 / 105011 | Data de início obrigatória / inválida | `422` |
| 105012 / 105013 | Data fim obrigatória / inválida | `422` |
| 105014 / 105015 | Documento do titular obrigatório / inválido | `422` |
| 105016 | Data fim deve ser maior ou igual à data de início | `422` — barrar localmente |
| 105801 | Operação não permitida — acesso negado | **Alerta crítico** de credencial/permissão; não retentar |
| 105802 | Operação não permitida — **opt-in não encontrado** | `403` — falta base autorizativa; barrar localmente via §7.1 |
| 105998 | **Fora da janela de processamento** | Enfileirar e reprocessar na próxima janela; **não** falhar a operação de negócio |
| 105999 | Erro inesperado | **Retentável** |

### 10.1 Validações locais (evitam a chamada)

`A01` documentos 11/14 dígitos com DV válido e zero-pad (105007, 105015) · `A02` `dataFim >= dataInicio` (105016) · `A03` listas não vazias (105004, 105008) · `A04` sem mistura de `99T` com valores específicos · `A05` arranjos no domínio vigente (105009) · `A06` `tipoAvaliacao` restrito aos valores de agenda · `A07` base autorizativa ativa cobrindo UFR × credenciadoras × arranjos × janela (105802) · `A08` rate limit de consulta online por UFR (§8.4) · `A09` carteira presente quando o participante é "Prestador de Serviço".

---

## 11. Observabilidade

**Métricas:** `agenda_consultas_total{modo,resultado}` · `agenda_cerc_latency_seconds{modo}` (SLO p95: batch < 3 s, online < 8 s) · `agenda_webhook_urs_total` · `agenda_webhook_orfas_total` · `agenda_arquivo_linhas_total{leiaute,resultado}` · `agenda_ur_frescor_horas` (histograma de `now() - dataHoraUltimaAtualizacao`).

**Alertas:**

| Condição | Severidade |
|---|---|
| `105801` em qualquer volume | **crítico** |
| Webhook respondido fora de 2xx | **crítico** (só há 5 tentativas) |
| `agenda_webhook_orfas_total` > 1 % das URs recebidas | alto (correlação §5.4 quebrada) |
| Taxa de rejeição de linhas > 0,5 % | alto |
| Arquivo AP005 diário não recebido até as 9h | alto |
| `105998` recorrente | médio (revisar janela de execução) |
| Consulta online sem base autorizativa ativa | **crítico** (compliance) |

---

## 12. Critérios de aceite e testes

### 12.1 Unitários

- Regras `A01`–`A09` com caso positivo e negativo cada.
- Parser AP005: linha completa com 12.16; linha **sem** 12.16 (layout antigo); layout reduzido "sem agenda"; linha sem bloco 12 (baixa sem pagamento).
- `tipoInformacaoPagamento` = `6` (liquidação) **não** interpretado como efeito de contrato.
- `tipoInformacaoPagamento` = `8` aceito sem exceção.
- `tipoConta` = `CG`/`CI` aceito no canal agenda e rejeitado no canal contratos (SPEC 02).
- `numeroConta` com hífen para `CC`, sem hífen para `PG`.
- Upsert por `dataHoraUltimaAtualizacao`: dado mais antigo **não** sobrescreve mais recente; empate resolve por precedência WEBHOOK > SINCRONO > ARQUIVO.
- Agregação de `posicao`: fumaça nunca somada nos totais de constituído.
- Correlação §5.4 com `99T` como universo; UR que casa duas consultas é anexada a ambas.

### 12.2 Integração

| # | Cenário | Esperado |
|---|---|---|
| IT-01 | Consulta BATCH válida | `200` com agendas; `valorTotalUR` preservado |
| IT-02 | Consulta ONLINE | `202` com síncrono; URs de webhook agregadas à mesma `consultaId` |
| IT-03 | ONLINE em homologação com credenciadora `72253725000100` | **webhook efetivamente disparado** (§5.6) |
| IT-04 | Quiet period expira | consulta vai a `COMPLETA` |
| IT-05 | 15 min sem fechar | `COMPLETA_COM_TIMEOUT` + alerta |
| IT-06 | Webhook sem consulta correlacionável | grava em `agenda_ur_orfa`, não descarta |
| IT-07 | Webhook duplicado | processado uma única vez |
| IT-08 | CERC retorna `105001` | `200` com lista vazia, **não** erro |
| IT-09 | CERC retorna `105802` | `403`; deve ter sido barrado antes por `A07` |
| IT-10 | CERC retorna `105998` | operação enfileirada, não falha |
| IT-11 | Consulta ONLINE sem base autorizativa | `403`, **sem** chamada à CERC |
| IT-12 | 11ª consulta online do mesmo UFR no dia | bloqueada por rate limit (`A08`) |
| IT-13 | Ingestão AP005 completo | linhas contabilizadas em `arquivo_agenda_processado` |
| IT-14 | Reprocessar o mesmo arquivo | idempotente, zero duplicação |
| IT-15 | Ingestão AP005A/AP005B `.gz` | descompacta por streaming; `origem_arquivo` correto |
| IT-16 | Arquivo com 3 linhas inválidas em 100k | 3 em `agenda_ur_rejeitada`, arquivo concluído |
| IT-17 | Arquivo "sem informações de agenda" | reconhecido como resultado válido |
| IT-18 | UR com 12.16 preenchido | junção com contrato da SPEC 02 resolve |

### 12.3 Carga

- Receptor de webhook: **500 req/s** por 5 min, p99 < 200 ms, 100 % de respostas 2xx.
- Ingestor de arquivo: 5 milhões de linhas com consumo de memória constante (streaming comprovado).

### 12.4 Definição de pronto

- [ ] Testes §12.1–12.3 verdes
- [ ] Catálogo `105xxx` mapeado 1:1 em enum, com teste de cobertura
- [ ] `105001` tratado como sucesso vazio (erro clássico de implementação)
- [ ] Nenhum `float`/`double` em campo monetário
- [ ] Trilha de compliance (§8) completa e relatório exportável
- [ ] Fluxo de webhook validado em homologação com os CNPJs de §5.6
- [ ] Parser tolerante a arquivo com e sem a coluna 12.16
- [ ] Decisão registrada sobre habilitar (ou não) a segregação AP005A/AP005B

---

## 13. Pontos a confirmar com a CERC

1. **Correlação consulta ↔ webhook** (§5.4): existe algum identificador de consulta no payload que não esteja documentado? Sem ele, a correlação é heurística.
2. **Sinal de fim** do envio de URs por webhook — existe evento de encerramento? Se sim, substitui o quiet period.
3. **Rate limits** de `POST /v15/agenda/consultar` (batch e online) e volume máximo por consulta.
4. **Janela de processamento** exata referida por `105998` para o canal API.
5. `tipoInformacaoPagamento = 8` (promessa de cessão) no **webhook** de agenda — a enumeração publicada vai até `7`, mas o arquivo aceita `8` (§6.3).
6. Horário-limite de disponibilização do arquivo AP005 diário (para calibrar o alerta das 9h).
7. Comportamento do campo 12.16 em reprocessamentos de datas anteriores a 03/11/2025.

---

## 14. Dependências entre as três specs

| Componente | Dono | Consumidores |
|---|---|---|
| `TokenProvider` (OAuth2) | SPEC 01 | SPEC 02, SPEC 03 |
| `webhook_inbox` + receptor HTTP | SPEC 01 | SPEC 02 (`contrato`), SPEC 03 (`agenda`) |
| `cerc_requisicao` (auditoria) | SPEC 01 | todas |
| `dominio_arranjo` | SPEC 01 | todas |
| Normalizador de documentos | SPEC 01 | todas |
| Base autorizativa (opt-in ativo) | SPEC 01 | **SPEC 03 valida antes de cada consulta** |
| `identificadorCercContrato` (col. 12.16) | SPEC 03 | junção com `contrato` da SPEC 02 |
| `valorLivre` / `valorTotalUR` | SPEC 03 | **SPEC 02 usa como base de dimensionamento de garantia** |

**Ordem de implementação recomendada:** SPEC 01 (token + webhook + opt-in) → **SPEC 03** (agenda: sem ela não há como dimensionar garantia) → SPEC 02 (contratos).

> Ajuste em relação à ordem sugerida anteriormente: a agenda precede o contrato porque `valorLivre` e `valorTotalUR` são os insumos para decidir **quanto** onerar. Registrar contrato sem visão de agenda produz `resultadoDistribuicaoOnus = 2` (insuficiente) de forma sistemática.
