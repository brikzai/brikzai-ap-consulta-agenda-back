import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-secret-key-change-in-production")
DEBUG = os.environ.get("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "cerc_shared",
    "agenda_ur",
    "politica_consulta",
]

MIDDLEWARE = []

ROOT_URLCONF = "agendaservice.urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "agenda_service"),
        "USER": os.environ.get("POSTGRES_USER", "agenda_service"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "agenda_service"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        # Schema `cerc` e' compartilhado e sempre visivel; `agenda` e' o schema
        # padrao (pool) usado fora do contexto de requisicao HTTP (testes,
        # comandos de manutencao). O roteamento por tenant real (schema
        # dedicado por financiador) e' implementado no Plano 2 via middleware.
        "OPTIONS": {"options": "-c search_path=agenda,cerc,public"},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

USE_TZ = True
TIME_ZONE = "UTC"
