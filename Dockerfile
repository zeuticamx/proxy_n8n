FROM python:3.12-slim

# Evita .pyc y hace que los logs salgan sin buffer (importante para ver
# las métricas de caché en tiempo real en los logs de EasyPanel).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# curl para el HEALTHCHECK. Si no lo quieres, borra esta línea y el HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements primero: si no cambia, Docker reutiliza esta capa
# y no reinstala dependencias en cada build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Usuario sin privilegios. El proxy ve las API keys que pasan en los headers,
# así que no lo corras como root.
# Nota: la imagen base python:3.12-slim ya trae un usuario llamado "proxy"
# (viene de Debian/_apt), por eso usamos otro nombre aquí.
RUN useradd -m -u 1001 appuser && chown -R appuser:appuser /app
USER appuser

ENV SYSTEM_TTL=5m \
    ENABLE_AUTO_CACHE=true \
    PORT=80

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:80/health || exit 1

# --timeout-keep-alive alto: los loops de tools del agente pueden tardar.
# proxy-headers para que EasyPanel/Traefik pase bien el esquema y la IP.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 120 --proxy-headers --forwarded-allow-ips '*'"]