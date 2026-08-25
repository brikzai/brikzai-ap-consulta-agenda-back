"""Publish no tópico de webhook_inbox — design §8. O handler HTTP
(apps/agenda/views.py) já gravou o evento cru em webhook_inbox ANTES de
chamar isto. Publicar aqui é melhor-esforço: se falhar (rede, projeto GCP
não configurado em dev, o que for), a linha já persistida continua lá com
processado_em IS NULL, e um job de varredura futuro a recuperaria. Por
isso esta função nunca deixa uma exceção escapar — ela só loga.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_publisher = None


def _get_publisher():
    global _publisher
    if _publisher is None:
        from google.cloud import pubsub_v1

        _publisher = pubsub_v1.PublisherClient()
    return _publisher


def _topic_path() -> str:
    project = os.environ["GOOGLE_CLOUD_PROJECT"]
    topic = os.getenv("PUBSUB_TOPIC_AGENDA_WEBHOOK", "agenda-webhook-inbox")
    return f"projects/{project}/topics/{topic}"


def publish_webhook_agenda(webhook_inbox_id: str, financiador_id: str) -> None:
    """Publica só os IDs (não o payload) — o consumidor busca o evento
    completo em webhook_inbox pelo id; a mensagem em si fica pequena e o
    payload nunca vive em dois lugares."""
    try:
        topic = _topic_path()
        data = json.dumps({
            "webhook_inbox_id": webhook_inbox_id,
            "financiador_id": financiador_id,
        }).encode("utf-8")
        future = _get_publisher().publish(topic, data)
        future.add_done_callback(lambda f: _log_publish_result(f, webhook_inbox_id))
    except Exception:
        logger.exception("[Pub/Sub] Falha ao publicar webhook_inbox_id=%s", webhook_inbox_id)


def _log_publish_result(future, webhook_inbox_id: str) -> None:
    try:
        future.result()
    except Exception:
        logger.exception("[Pub/Sub] Publish assíncrono falhou webhook_inbox_id=%s", webhook_inbox_id)
