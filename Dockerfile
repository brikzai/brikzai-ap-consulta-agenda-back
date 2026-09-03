FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=8080
# --timeout: sem isto o gunicorn usa o default de 30s e mata o worker no
# meio de uma consulta ONLINE que insere muitas URs em série (achado ao
# testar contra a CERC real, docs/runbooks/gcp-setup.md) — mesmo valor do
# --timeout do Cloud Run (cloudbuild.yaml). ${WEB_CONCURRENCY:-2} (não um
# número fixo): mesmo padrão de ap-back-optin.
CMD exec gunicorn config.wsgi:application --bind :$PORT --workers ${WEB_CONCURRENCY:-2} --timeout 60
