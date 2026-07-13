FROM node:22-alpine AS frontend

WORKDIR /build

COPY package*.json ./
RUN if [ -f package-lock.json ]; then \
        npm ci --no-audit --no-fund; \
    else \
        npm install --no-audit --no-fund; \
    fi

COPY assets ./assets
COPY research/templates ./research/templates
RUN npm run build


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y curl libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY atlasmacro ./atlasmacro
COPY research ./research
COPY manage.py ./manage.py
COPY --from=frontend /build/research/static/research/css ./research/static/research/css
COPY --from=frontend /build/research/static/research/js ./research/static/research/js

RUN pip install --upgrade pip \
    && pip install .

RUN addgroup --system django \
    && adduser --system --ingroup django django \
    && mkdir -p /app/staticfiles /app/media /app/data/artifacts \
    && chown -R django:django /app

USER django

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail http://127.0.0.1:8000/healthz/ || exit 1

CMD ["gunicorn", "atlasmacro.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "60", "--access-logfile", "-"]
