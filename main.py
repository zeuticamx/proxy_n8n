"""
Proxy de caching para Anthropic <- n8n
=======================================

n8n (lmChatAnthropic) no expone cache_control. Este proxy se pone en medio
e inyecta los breakpoints antes de reenviar a la API real.

Uso en n8n:
    Credentials > Anthropic account > Base URL:
        https://tu-proxy.easypanel.host

    n8n llamará a  https://tu-proxy.../v1/messages  y el proxy reenvía a
    https://api.anthropic.com/v1/messages con cache_control inyectado.

Estrategia de breakpoints (max 4 disponibles):
    1. Explícito al final de `system`  -> cachea tools + system.
       Es el prefijo COMPARTIDO entre todos los clientes de WhatsApp.
    2. Automático (cache_control top-level) -> cachea el historial que crece.
       n8n manda contextWindowLength=200, así que esto importa.

Deploy en EasyPanel:
    pip install fastapi uvicorn httpx
    uvicorn anthropic_cache_proxy:app --host 0.0.0.0 --port 8000
"""

import os
import json
import logging

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse

UPSTREAM = "https://api.anthropic.com"

# TTL del breakpoint explícito de system+tools.
# "5m" es suficiente si tienes tráfico constante (se refresca gratis en cada uso).
# "1h" si tienes huecos largos entre conversaciones (cuesta 2x el write, pero
# los reads siguen a 0.1x).
SYSTEM_TTL = os.getenv("SYSTEM_TTL", "5m")

# Cachear también el historial que crece (automatic caching).
ENABLE_AUTO_CACHE = os.getenv("ENABLE_AUTO_CACHE", "true").lower() == "true"

# Mínimos por modelo (tokens). Si el prefijo no llega, la API ignora el
# cache_control en silencio -- no falla, simplemente no cachea.
MIN_TOKENS = {
    "claude-haiku-4-5": 4096,
    "claude-sonnet-5": 1024,
    "claude-opus-5": 512,
}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cache-proxy")

app = FastAPI(title="Anthropic Cache Proxy")
client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(600.0))


def inject_cache_control(body: dict) -> dict:
    """Inserta breakpoints de caché en el body antes de reenviarlo."""

    system = body.get("system")

    # n8n/langchain manda `system` como string. La API acepta string o lista
    # de content blocks, pero cache_control solo va en bloques.
    if isinstance(system, str) and system.strip():
        body["system"] = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral", "ttl": SYSTEM_TTL},
            }
        ]

    elif isinstance(system, list) and system:
        # Ya viene como bloques: marca el ÚLTIMO.
        # El hash es acumulativo, así que esto cachea tools + todo system.
        for blk in system:
            blk.pop("cache_control", None)
        system[-1]["cache_control"] = {"type": "ephemeral", "ttl": SYSTEM_TTL}

    # Automatic caching para el historial. La doc advierte que si el breakpoint
    # explícito de system usa un TTL distinto al top-level, la API devuelve 400
    # cuando caen en el mismo bloque -- aquí no pasa porque el explícito está en
    # `system` y el automático cae en `messages`. Aun así usamos el mismo TTL.
    if ENABLE_AUTO_CACHE and body.get("messages"):
        body["cache_control"] = {"type": "ephemeral", "ttl": SYSTEM_TTL}

    return body


def log_usage(payload: dict) -> None:
    """Loguea métricas de caché para que puedas medir el ahorro real."""
    usage = payload.get("usage") or {}
    if not usage:
        return

    read = usage.get("cache_read_input_tokens", 0)
    write = usage.get("cache_creation_input_tokens", 0)
    fresh = usage.get("input_tokens", 0)
    total = read + write + fresh

    if total:
        hit_rate = 100 * read / total
        log.info(
            "cache read=%s write=%s fresh=%s total=%s hit=%.1f%%",
            read, write, fresh, total, hit_rate,
        )
        if read == 0 and write == 0:
            log.warning(
                "Sin caché. Causas típicas: prefijo bajo el mínimo del modelo, "
                "o algo variable (timestamp/wa_id) dentro del prefijo."
            )


@app.get("/health")
async def health():
    return {"ok": True, "ttl": SYSTEM_TTL, "auto_cache": ENABLE_AUTO_CACHE}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def passthrough(path: str, request: Request):
    """
    Reenvía CUALQUIER ruta a la API real de Anthropic.

    Necesario porque n8n no solo llama a /v1/messages: el test de conexión
    de la credencial golpea otro endpoint (p.ej. /v1/models) para validar
    la API key. Si el proxy solo conociera /v1/messages, ese test fallaría
    con 404 aunque la key sea válida -- que es justo lo que pasó.

    Solo se inyecta cache_control cuando la ruta es /v1/messages con un
    body JSON; todo lo demás (GET /v1/models, etc.) pasa intacto.
    """
    raw = await request.body()
    upstream_path = f"/{path}"

    body = None
    if raw and upstream_path == "/v1/messages":
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = None

        if body is not None:
            body = inject_cache_control(body)
            raw = json.dumps(body).encode()

    # Reenvía las credenciales del cliente (x-api-key, anthropic-version).
    # Quitamos host/content-length porque httpx los recalcula.
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "accept-encoding")
    }

    is_stream = bool(body and body.get("stream"))

    if is_stream:
        req = client.build_request(
            request.method, upstream_path, content=raw,
            params=request.query_params, headers=headers,
        )
        upstream = await client.send(req, stream=True)

        async def relay():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            relay(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
        )

    upstream = await client.request(
        request.method, upstream_path,
        content=raw if raw else None,
        params=request.query_params,
        headers=headers,
    )

    if upstream_path == "/v1/messages":
        try:
            payload = upstream.json()
            log_usage(payload)
            return JSONResponse(payload, status_code=upstream.status_code)
        except Exception:
            pass

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type"),
    )


@app.on_event("shutdown")
async def shutdown():
    await client.aclose()
