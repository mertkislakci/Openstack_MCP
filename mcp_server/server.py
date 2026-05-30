"""
OpenStack MCP Server
════════════════════
Desteklenen transportlar (MCP_TRANSPORT env var):

  streamable-http  → Tek HTTP bağlantısı, POST /mcp  [DEFAULT]
                     MCP spec'in güncel önerdiği yöntem.
                     Bearer token auth: MCP_AUTH_TOKEN env var.

  stdio            → stdin/stdout pipe — Claude Desktop, local dev.

  sse              → Eski SSE transport (geriye dönük uyumluluk).
                     Yeni deploymentlarda kullanma.

streamable-http endpoints:
  POST /mcp               → MCP protocol mesajları (tools/list, tools/call …)
  GET  /health            → liveness probe
  GET  /feedback          → SSE stream — real-time tool execution events
  GET  /feedback/recent   → son N event JSON (polling)
  GET  /feedback/context  → LLM sistem prompt'una hazır metin

Startup:
  1. Config parse  2. structlog  3. Lazy ToolRegistry  4. Transport
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import structlog
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    ListToolsRequest,
    ListToolsResult,
    TextContent,
    Tool,
)

from core.config import get_settings
from core.feedback import get_feedback_bus
from mcp_server.registry import get_registry

log = structlog.get_logger(__name__)


def _configure_logging() -> None:
    import logging
    cfg = get_settings()
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, cfg.log_level, logging.INFO),
    )
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


# ── MCP Server instance ───────────────────────────────────────────────────────

server = Server("openstack-mcp")


@server.list_tools()
async def handle_list_tools(request: ListToolsRequest) -> ListToolsResult:
    registry = get_registry()
    tools = [
        Tool(
            name=d["name"],
            description=d["description"],
            inputSchema=d["inputSchema"],
        )
        for d in registry.list_definitions()
    ]
    log.info("tools listed", count=len(tools))
    return ListToolsResult(tools=tools)


@server.call_tool()
async def handle_call_tool(request: CallToolRequest) -> CallToolResult:
    tool_name = request.params.name
    arguments: dict[str, Any] = request.params.arguments or {}

    log.info("tool call received", tool=tool_name, args=list(arguments.keys()))

    registry = get_registry()
    try:
        tool = registry.get(tool_name)
    except KeyError:
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {tool_name!r}"}))],
            isError=True,
        )

    result = await tool(**arguments)
    return CallToolResult(
        content=[TextContent(type="text", text=result.to_mcp_text())],
        isError=not result.success,
    )


# ── Shared init options ───────────────────────────────────────────────────────

def _init_options() -> InitializationOptions:
    return InitializationOptions(
        server_name="openstack-mcp",
        server_version="0.1.0",
        capabilities=server.get_capabilities(
            notification_options=None,
            experimental_capabilities=None,
        ),
    )


# ── Transport: stdio ──────────────────────────────────────────────────────────

async def run_stdio() -> None:
    """stdin/stdout transport — Claude Desktop veya pipe kullanımı için."""
    from mcp.server.stdio import stdio_server

    log.info("MCP stdio transport starting")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, _init_options())


# ── Transport: streamable-http ────────────────────────────────────────────────

async def run_streamable_http() -> None:
    """
    Streamable HTTP transport — MCP'nin güncel standart transport'u.

    SSE'den farkı:
      SSE:             GET /sse (açık kalır)  +  POST /messages (ayrı)
      streamable-http: POST /mcp  (tek endpoint, aynı bağlantıdan stream döner)

    Auth:
      MCP_AUTH_TOKEN set edilmişse her isteğe
      "Authorization: Bearer <token>" header'ı zorunludur.
      Boş bırakılırsa auth devre dışı (local/dev için).
    """
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    cfg = get_settings()

    # ── Session manager ───────────────────────────────────────────────────────
    # Her LLM oturumu için bağımsız MCP session yönetir.
    # stateless=False: oturum durumu bellekte tutulur.
    session_manager = StreamableHTTPSessionManager(
        app=server,
        event_store=None,   # Gerekirse Redis-backed EventStore eklenebilir
        json_response=False,
        stateless=False,
    )

    app = FastAPI(title="OpenStack MCP Server", version="0.1.0")

    # ── Bearer token middleware ───────────────────────────────────────────────
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next: Any) -> Response:
        token = cfg.mcp_auth_token
        # Auth yalnızca /mcp endpoint'inde aktif, sağlık/feedback hariç
        if token and request.url.path == "/mcp":
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer ") or auth_header[7:] != token:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Invalid or missing Bearer token"},
                )
        return await call_next(request)

    # ── MCP endpoint (streamable-http) ────────────────────────────────────────
    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"])
    async def mcp_endpoint(request: Request) -> Any:
        """
        Tek endpoint — LLM buraya bağlanır.
        GET  → yeni MCP session başlatır (SSE benzeri event stream)
        POST → tool çağrısı veya başka MCP mesajı gönderir
        DELETE → oturumu sonlandırır
        """
        await session_manager.handle_request(
            request.scope,
            request.receive,
            request._send,  # type: ignore[attr-defined]
        )

    # ── Warm-up (arka planda) ────────────────────────────────────────────────
    @app.on_event("startup")
    async def _start_warmup() -> None:
        from mcp_server.warmup import warm_cache
        asyncio.create_task(warm_cache(delay=2.0))

    # ── Health ────────────────────────────────────────────────────────────────
    @app.get("/health")
    async def health() -> JSONResponse:
        registry = get_registry()
        from core.circuit_breaker import all_breaker_statuses
        return JSONResponse({
            "status": "ok",
            "transport": "streamable-http",
            "tools_loaded": registry.loaded_count,
            "tools_registered": len(registry.names),
            "auth_enabled": bool(cfg.mcp_auth_token),
            "circuit_breakers": all_breaker_statuses(),
        })

    @app.get("/metrics")
    async def metrics_endpoint() -> Any:
        from core.metrics import metrics_response, update_breaker_gauges
        from fastapi.responses import PlainTextResponse
        update_breaker_gauges()
        body, ctype = metrics_response()
        return PlainTextResponse(body, media_type=ctype)

    @app.get("/status/cache")
    async def cache_status() -> JSONResponse:
        from core.cache import get_cache
        c = get_cache()
        return JSONResponse({
            "size": len(c),
            "max_size": c._max_size,
            "hits": c.metrics.hits,
            "misses": c.metrics.misses,
            "stale_hits": c.metrics.stale_hits,
            "evictions": c.metrics.evictions,
            "hit_rate": round(c.metrics.hit_rate, 3),
            "stampedes_blocked": c.metrics.stampedes_blocked,
        })

    # ── Feedback endpoints ────────────────────────────────────────────────────
    @app.get("/feedback")
    async def feedback_stream() -> StreamingResponse:
        """Real-time SSE stream — LLM tool çıktılarını izler."""
        bus = get_feedback_bus()
        q = bus.subscribe()

        async def _gen():
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=30.0)
                        yield f"data: {event.model_dump_json()}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(_gen(), media_type="text/event-stream")

    @app.get("/feedback/recent")
    async def feedback_recent(n: int = 20) -> JSONResponse:
        """Son N tool execution event — polling için."""
        events = await get_feedback_bus().get_recent(n)
        return JSONResponse([e.model_dump() for e in events])

    @app.get("/feedback/context")
    async def feedback_context(n: int = 10) -> JSONResponse:
        """LLM sistem prompt'una eklenmeye hazır metin."""
        ctx = await get_feedback_bus().get_llm_context(n)
        return JSONResponse({"context": ctx})

    # ── Başlat ────────────────────────────────────────────────────────────────
    config = uvicorn.Config(
        app,
        host=cfg.mcp_host,
        port=cfg.mcp_port,
        log_level=cfg.log_level.lower(),
    )
    srv = uvicorn.Server(config)

    log.info(
        "MCP streamable-http server starting",
        host=cfg.mcp_host,
        port=cfg.mcp_port,
        endpoint=f"http://{cfg.mcp_host}:{cfg.mcp_port}/mcp",
        auth_enabled=bool(cfg.mcp_auth_token),
    )

    # session_manager'ın kendi lifecycle'ını yönet
    async with session_manager.run():
        await srv.serve()


# ── Transport: SSE (legacy) ───────────────────────────────────────────────────

async def run_sse() -> None:
    """
    Eski SSE transport — geriye dönük uyumluluk için saklandı.
    Yeni kurulumlarda streamable-http kullan.
    """
    import uvicorn
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from mcp.server.sse import SseServerTransport

    cfg = get_settings()
    log.warning("SSE transport is legacy — migrate to streamable-http")

    app = FastAPI(title="OpenStack MCP Server (SSE/legacy)", version="0.1.0")
    sse_transport = SseServerTransport("/messages")

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "transport": "sse-legacy"})

    @app.get("/feedback/recent")
    async def feedback_recent(n: int = 20) -> JSONResponse:
        events = await get_feedback_bus().get_recent(n)
        return JSONResponse([e.model_dump() for e in events])

    @app.get("/feedback/context")
    async def feedback_context(n: int = 10) -> JSONResponse:
        ctx = await get_feedback_bus().get_llm_context(n)
        return JSONResponse({"context": ctx})

    @app.get("/sse")
    async def sse_endpoint(request: Request) -> Any:
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send  # type: ignore[attr-defined]
        ) as streams:
            await server.run(streams[0], streams[1], _init_options())

    @app.post("/messages")
    async def message_endpoint(request: Request) -> Any:
        return await sse_transport.handle_post_message(
            request.scope, request.receive, request._send  # type: ignore[attr-defined]
        )

    config = uvicorn.Config(app, host=cfg.mcp_host, port=cfg.mcp_port, log_level=cfg.log_level.lower())
    await uvicorn.Server(config).serve()


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    _configure_logging()
    cfg = get_settings()

    log.info(
        "OpenStack MCP Server starting",
        transport=cfg.mcp_transport,
        tools=get_registry().names,
    )

    transport = cfg.mcp_transport.lower().replace("_", "-")

    if transport == "stdio":
        asyncio.run(run_stdio())
    elif transport == "streamable-http":
        asyncio.run(run_streamable_http())
    elif transport == "sse":
        asyncio.run(run_sse())
    else:
        print(
            f"Unknown transport: {transport!r}. "
            "Use 'streamable-http' (recommended), 'stdio', or 'sse' (legacy).",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
