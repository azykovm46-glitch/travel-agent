# Образ агента: API + запасной веб-интерфейс на /. Секреты — только через переменные окружения хостинга.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PORT=8000 MEMORY_DB=/tmp/profiles.sqlite3 TOOLS_BACKEND=tools.mock_backend
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home agent && chown -R agent /app
USER agent

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://localhost:{os.environ[\"PORT\"]}/api/health', timeout=4)"
# Render и Railway передают порт в $PORT
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT}"]
