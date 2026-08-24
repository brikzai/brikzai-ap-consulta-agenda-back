# agenda-service — Plan 01: Django Project Scaffold — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the Django project skeleton (no ORM) with a working health endpoint — the base every other plan builds on.

**Architecture:** Django with `DATABASES = {}` (no Django ORM/migrations, per house convention — see design doc §0/§3, mirroring `ap-back-optin` and `ap-back-contratos`). Single app `apps.agenda`. Function-based views, JSON responses — this service is a pure JSON API consumed by a separate frontend, no templates/admin.

**Tech Stack:** Python 3.12, Django >=4.2,<5.0, djangorestframework (JSON renderer only), python-dotenv, pytest, pytest-django, gunicorn.

**Spec:** `docs/superpowers/specs/2026-08-24-agenda-service-design.md` (§3, §4). Series: plan 1 of ~10 (`agenda-plan-01` scaffold, `02` schema, `03` shared data-access, `04` auth, `05` upsert repository, `06` cerc client + validation, `07` webhook + correlation, `08` file ingestion, `09` remaining API + compliance, `10` observability + load tests), each independently reviewable/testable. Plans 03+ are written after this one lands.

## Global Constraints

- `TIME_ZONE = "America/Sao_Paulo"`, `USE_TZ = True`.
- Secrets never committed; `.env` is git-ignored (already in `.gitignore`), `.env.example` holds only keys.
- No Django ORM: `DATABASES = {}` — data access goes through `shared/cloudsql_client.py` (Plan 03), not through this plan.
- No DRF ViewSets/admin/templates — this is a JSON-only API consumed by a separate frontend app.
- Money columns are `NUMERIC(18,2)` / `decimal.Decimal`, never `float`/`double` (applies from Plan 02 onward — noted here since it's a project-wide rule, nothing in this plan touches money yet).

---

### Task 1: Django project scaffold

**Files:**
- Create: `manage.py`
- Create: `config/__init__.py`
- Create: `config/settings.py`
- Create: `config/urls.py`
- Create: `config/wsgi.py`
- Create: `apps/__init__.py`
- Create: `apps/agenda/__init__.py`
- Create: `apps/agenda/views.py`
- Create: `apps/agenda/urls.py`
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `pytest.ini`
- Test: `apps/agenda/tests/test_health.py`
- (`.gitignore` already exists — no change needed here.)

**Interfaces:**
- Produces: `GET /api/v1/health` → `200 {"status": "ok"}`. Every later plan's URLs mount under `config.urls` → `apps.agenda.urls`.

- [ ] **Step 1: Write `requirements.txt`**

```
django>=4.2,<5.0
djangorestframework>=3.15
django-cors-headers
python-dotenv
sqlalchemy>=2.0
pg8000
cloud-sql-python-connector[pg8000]
google-cloud-secret-manager
httpx
pyjwt[crypto]>=2.8
python-ulid
pytest
pytest-django
respx
gunicorn
```

- [ ] **Step 2: Write `.env.example`**

```
ENVIRONMENT=development
DJANGO_SECRET_KEY=dev-secret-key-change-in-production
ALLOWED_HOSTS=*

# Front separado consome esta API (design doc §3.6/§10) — vazio por padrão
# (nenhuma origem liberada) até ser configurado; em dev local aponta pro
# Vite, mesmo padrão do ap-back-contratos.
CORS_ALLOWED_ORIGINS=http://localhost:5173

# Usados só por scripts/apply_schema.py (Plan 02) — um alvo fixo por vez,
# não passa pela abstração de tenant do app em runtime (mesmo padrão do
# ap-back-contratos: aplicar schema é uma operação manual, uma instancia
# de cada vez).
CLOUDSQL_CONNECTION_NAME=
CLOUDSQL_DB_USER=
CLOUDSQL_DB_PASSWORD=
CLOUDSQL_DB_NAME=

# JWT do IdP corporativo (shared/jwt_auth.py, Plan 04) — mesmas chaves que
# ap-back-optin e ap-back-contratos já usam.
IAM_JWT_PUBLIC_KEY=
IAM_JWT_ISSUER=

# Config por tenant, usada pelo app em runtime (shared/tenant_config.py,
# Plan 03) — um segredo por financiador. Em dev, o tenant fixo reaproveita
# o mesmo CNPJ que ap-back-optin usa (12345678000199), ver design doc §14.
TENANT_12345678000199_CONFIG=

# Host OAuth da CERC (homologação) — não varia por tenant.
CERC_AUTH_URL=https://api.int.cerc.com/oauth/token
CERC_API_BASE_URL=https://ap-homolog.cerc.inf.br
```

- [ ] **Step 3: Write `config/settings.py`**

```python
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-secret-key-change-in-production")
DEBUG = os.getenv("ENVIRONMENT", "development").lower() != "production"
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "*").split(",") if h.strip()]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "corsheaders",
    "apps.agenda",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# Sem Django ORM — dados via shared.cloudsql_client (design doc §3, Plan 03).
DATABASES = {}

# Front separado consome esta API (design doc §3.6/§10).
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173").split(",") if o.strip()
]

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_TZ = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"standard": {"format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "standard"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
```

- [ ] **Step 4: Write `config/urls.py`**

```python
from django.urls import path, include

urlpatterns = [
    path("api/v1/", include("apps.agenda.urls")),
]
```

- [ ] **Step 5: Write `config/wsgi.py`**

```python
import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_wsgi_application()
```

- [ ] **Step 6: Write `config/__init__.py`, `apps/__init__.py`, `apps/agenda/__init__.py`**

Empty files, all three.

- [ ] **Step 7: Write `apps/agenda/views.py`**

```python
from django.http import JsonResponse


def health(request):
    return JsonResponse({"status": "ok"})
```

- [ ] **Step 8: Write `apps/agenda/urls.py`**

```python
from django.urls import path
from . import views

urlpatterns = [
    path("health", views.health),
]
```

- [ ] **Step 9: Write `manage.py`**

```python
#!/usr/bin/env python
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
```

- [ ] **Step 10: Write `pytest.ini`**

```ini
[pytest]
DJANGO_SETTINGS_MODULE = config.settings
python_files = tests.py test_*.py *_tests.py
```

- [ ] **Step 11: Write the failing test**

```python
# apps/agenda/tests/test_health.py
# Sem @pytest.mark.django_db / import pytest: este projeto nao usa Django
# ORM (DATABASES = {}, design doc §3), e o marker django_db tenta acessar
# connections['default'], que quebra com DATABASES vazio.
from django.test import Client


def test_health_returns_ok():
    response = Client().get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

Create empty `apps/agenda/tests/__init__.py` alongside it.

- [ ] **Step 12: Install deps and run test**

Run: `pip install -r requirements.txt` then `pytest apps/agenda/tests/test_health.py -v`
Expected: PASS (this is a scaffold smoke test — there is no prior "red" state to observe since `health` has no logic to break; verifying it passes on first run confirms the scaffold is wired correctly).

- [ ] **Step 13: Write `Dockerfile` and `.dockerignore`**

`.dockerignore` (copiado de `ap-back-contratos` — sem isso, `COPY . .` empacota `.env` com credenciais reais do Cloud SQL na imagem):

```
.git/
.venv/
__pycache__/
*.pyc
.env
.pytest_cache/
docs/
.superpowers/
logs/
*.pem
```

`Dockerfile`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
CMD exec gunicorn config.wsgi:application --bind :$PORT --workers 4
```

- [ ] **Step 14: Commit**

```bash
git add manage.py config apps requirements.txt .env.example Dockerfile .dockerignore pytest.ini
git commit -m "feat: scaffold Django project (no ORM), health endpoint"
```

---

## Self-Review Notes

- **Spec coverage:** design doc §3 (stack decision), §4 (folder layout), §10 (health-style JSON API for a separate frontend) → fully covered for what a scaffold plan owns.
- **Placeholder scan:** none — every step has runnable code.
- **Type consistency:** N/A (first plan in the series — nothing to be consistent with yet, mirrors `ap-back-contratos`' Plan 01 exactly in shape). Exports `config.urls`/`config.settings` module paths and `apps.agenda` app label that every later plan's `INSTALLED_APPS`/`urlpatterns` entries build on.

**Next:** `2026-08-24-agenda-plan-02-schema.md` (schema applied to the real `agenda` database, already provisioned on the `app-db` Cloud SQL instance).
