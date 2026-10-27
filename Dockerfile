FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
ARG TM_WEB_BASE=/TeamManager/
ENV TM_WEB_BASE=$TM_WEB_BASE
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd -u 10001 -m app
COPY backend/ backend/
COPY migrations/ migrations/
COPY scripts/ scripts/
COPY alembic.ini ./
COPY --from=frontend /build/dist/ frontend/dist/
RUN mkdir -p /app/data /app/backups && chown -R app:app /app/data /app/backups
USER app
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=3)"
CMD ["uvicorn", "backend.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8765", "--no-access-log"]
