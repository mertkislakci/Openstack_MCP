"""
MCP Proxy Server
════════════════
LLM ile upstream MCP server(lar) arasındaki ara katman.

SSE → streamable-http geçişindeki değişiklikler
────────────────────────────────────────────────
  Eski SSE:
    GET  upstream/sse          (açık event stream)
    POST upstream/messages     (ayrı mesaj kanalı)

  Yeni streamable-http:
    POST upstream/mcp          (tek endpoint — request + response stream)

Proxy'nin LLM'e sunduğu arayüz (proxy kendi /mcp endpoint'ini açar):
  POST /mcp                → upstream'e yönlendirir (streamable-http geçişi)
  GET  /health             → proxy liveness
  GET  /status             → upstream sağlık + metrik
  GET  /feedback/context   → tüm upstream'lerden birleşik LLM context
  GET  /feedback/recent    → tüm upstream'lerden birleşik events

Upstream iletişimi
──────────────────
  tools/list  → upstream /mcp üzerinden MCP initialize + tools/list akışı yerine
                daha basit: upstream /health + /feedback/recent kullanılır.
                Gerçek tool routing streamable-http proxy pass-through ile yapılır.

  Bir LLM bağlantısı geldiğinde:
    1. Router sağlıklı bir upstream seçer
    2. Proxy, LLM'in bağlantısını seçilen upstream'e tüneller (HTTP proxy)
    3. Upstream ile LLM doğrudan streamable-http konuşur
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from typing import Any

import httpx
import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from core.config import get_settings
from mcp_proxy.middleware import (
    AccessLogMiddleware,
    ApiKeyMiddleware,
    RateLimitMiddleware,
    RequestIDMiddleware,
)
from mcp_proxy.router import RoutingStrategy, UpstreamRouter

log = structlog.get_logger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    router: UpstreamRouter = app.state.router
    http: httpx.AsyncClient = app.state.http
    await router.start_health_checks()
    log.info("MCP Proxy started", upstreams=len(router._servers))
    yield
    await router.stop()
    await http.aclose()
    log.info("MCP Proxy stopped")


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    cfg = get_settings()

    app = FastAPI(
        title="OpenStack MCP Proxy",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.state.router = UpstreamRouter(
        urls=cfg.proxy_upstream_urls,
        strategy=RoutingStrategy.ROUND_ROBIN,
    )
    # Uzun süreli streaming bağlantılar için timeout=None
    app.state.http = httpx.AsyncClient(timeout=None)

    # ── Middleware ─────────────────────────────────────────────────────────
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RateLimitMiddleware, rate=50.0, capacity=100.0)
    app.add_middleware(ApiKeyMiddleware, api_keys=[])
    app.add_middleware(RequestIDMiddleware)

    # ── Routes ─────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "openstack-mcp-proxy"})

    @app.get("/status")
    async def status(request: Request) -> JSONResponse:
        router: UpstreamRouter = request.app.state.router
        return JSONResponse({"upstreams": router.status()})

    # ── MCP streamable-http pass-through ───────────────────────────────────
    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
    async def mcp_proxy(request: Request) -> Response:
        """
        LLM buraya bağlanır — seçilen upstream'e tam olarak tünellenir.

        streamable-http protokolü:
          GET    /mcp  → upstream'e ilet, event stream olarak döndür
          POST   /mcp  → upstream'e ilet, response stream olarak döndür
          DELETE /mcp  → oturum sonlandırma, upstream'e ilet
        """
        router: UpstreamRouter = request.app.state.router
        http: httpx.AsyncClient = request.app.state.http

        # Tool adını body'den okumaya gerek yok — router default seçer
        # (ilerleyen aşamada body parse edip tool_affinity yapılabilir)
        upstream = router.pick("default")
        target_url = f"{upstream.url}/mcp"

        # İstek header'larını kopyala, host'u yeniden yaz
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length")
        }
        headers["X-Forwarded-For"] = request.client.host if request.client else "unknown"
        headers["X-Request-ID"] = getattr(request.state, "request_id", "")

        # Upstream'e bearer token ekle (varsa)
        upstream_token = cfg.mcp_auth_token
        if upstream_token:
            headers["Authorization"] = f"Bearer {upstream_token}"

        body = await request.body()

        try:
            # stream=True ile upstream'den gelen byte stream'i LLM'e aktar
            req = http.build_request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
            resp = await http.send(req, stream=True)

            router.record_result(upstream, success=resp.status_code < 500)

            async def _stream():
                try:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
                finally:
                    await resp.aclose()

            return StreamingResponse(
                _stream(),
                status_code=resp.status_code,
                headers=dict(resp.headers),
                media_type=resp.headers.get("content-type"),
            )

        except httpx.ConnectError:
            router.record_result(upstream, success=False)
            raise HTTPException(
                status_code=503,
                detail=f"Upstream {upstream.url} bağlantı reddetti",
            )
        except Exception as exc:
            router.record_result(upstream, success=False)
            log.exception("mcp proxy error", upstream=upstream.url)
            raise HTTPException(status_code=502, detail=str(exc))

    # ── Feedback aggregation ───────────────────────────────────────────────
    @app.get("/feedback/context")
    async def feedback_context(request: Request, n: int = 10) -> JSONResponse:
        """Tüm upstream'lerden feedback context topla ve birleştir."""
        router: UpstreamRouter = request.app.state.router
        http: httpx.AsyncClient = request.app.state.http

        tasks = [
            _fetch_json(http, f"{s.url}/feedback/context", params={"n": n})
            for s in router._servers if s.healthy
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        merged = "\n\n".join(
            r.get("context", "") for r in results
            if isinstance(r, dict) and r.get("context")
        )
        return JSONResponse({"context": merged or "No feedback available."})

    @app.get("/feedback/recent")
    async def feedback_recent(request: Request, n: int = 20) -> JSONResponse:
        """Tüm upstream'lerden son N event, timestamp'e göre sıralı."""
        router: UpstreamRouter = request.app.state.router
        http: httpx.AsyncClient = request.app.state.http

        tasks = [
            _fetch_json(http, f"{s.url}/feedback/recent", params={"n": n})
            for s in router._servers if s.healthy
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        events: list[dict[str, Any]] = []
        for res in results:
            if isinstance(res, list):
                events.extend(res)
        events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        return JSONResponse(events[:n])

    return app


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _fetch_json(
    http: httpx.AsyncClient,
    url: str,
    params: dict[str, Any] | None = None,
) -> Any:
    try:
        r = await http.get(url, params=params, timeout=5.0)
        return r.json()
    except Exception as exc:
        log.warning("upstream fetch failed", url=url, error=str(exc))
        return {}


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    import logging
    cfg = get_settings()
    logging.basicConfig(stream=sys.stderr, level=getattr(logging, cfg.log_level, logging.INFO))

    import structlog
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

    log.info(
        "MCP Proxy starting",
        host=cfg.proxy_host,
        port=cfg.proxy_port,
        upstreams=cfg.proxy_upstream_urls,
    )
    uvicorn.run(
        create_app(),
        host=cfg.proxy_host,
        port=cfg.proxy_port,
        log_level=cfg.log_level.lower(),
    )


if __name__ == "__main__":
    main()
