"""Hash de deduplicação de webhooks — SPEC03 §5.3: dedupe por (tipoEvento,
hash canônico do evento, dataHoraEvento). A CERC reentrega o mesmo evento
em até 5 tentativas quando não recebe 2xx; reentrega deve ser inofensiva."""

import hashlib
import json


def hash_evento(tipo_evento: str, evento: dict, data_hora_evento: str) -> str:
    canonico = json.dumps(evento, sort_keys=True, ensure_ascii=False, default=str)
    chave = f"{tipo_evento}|{data_hora_evento}|{canonico}"
    return hashlib.sha256(chave.encode("utf-8")).hexdigest()
