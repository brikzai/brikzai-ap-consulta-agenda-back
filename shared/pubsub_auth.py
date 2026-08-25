"""Verificação OIDC do push subscription do Pub/Sub — design §6 ("Push
subscription bate em endpoint próprio, verificado por OIDC"). O Pub/Sub
assina cada requisição de push com um ID token OIDC do Google, emitido
para a conta de serviço configurada na subscription; verificamos aqui que
o token é genuíno e foi emitido para esta audiência específica — defesa
em profundidade além do IAM do próprio Cloud Run (roles/run.invoker
restrito à conta do Pub/Sub). O mesmo mecanismo protege o job de
completude (Task 4) disparado por Cloud Scheduler — o nome do módulo
menciona só Pub/Sub, mas a verificação é genérica.
"""

import logging
import os

logger = logging.getLogger(__name__)


def _verificar_id_token(token: str, audiencia: str) -> dict:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, GoogleAuthRequest(), audience=audiencia)


def verificar_push_oidc(request) -> bool:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer "):
        return False
    token = header[len("Bearer "):]

    try:
        audiencia = os.environ["PUBSUB_PUSH_AUDIENCE"]
        claims = _verificar_id_token(token, audiencia)
    except Exception:
        logger.warning("[Webhook] Token OIDC do push inválido ou PUBSUB_PUSH_AUDIENCE não configurado")
        return False

    conta_esperada = os.getenv("PUBSUB_PUSH_INVOKER_SA")
    if conta_esperada and claims.get("email") != conta_esperada:
        logger.warning("[Webhook] Token OIDC de conta inesperada: %s", claims.get("email"))
        return False

    return True
