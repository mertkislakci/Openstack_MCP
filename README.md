# openstack-mcp

> OpenStack operations via Model Context Protocol — async, lazy-import, LLM-feedback ready.

## Mimari

```
LLM (Claude / OpenAI / …)
        │  tool calls
        ▼
  MCP Proxy  :8000          ← auth · rate-limit · routing · load-balancing
        │  HTTP/JSON
        ▼
  MCP Server :8080          ← tool registry · lazy imports · cache
        │  openstacksdk
        ▼
  OpenStack API             ← Nova · Keystone · Neutron · Cinder
```

Tüm komut çıktıları **Feedback Bus**'a yazılır ve `/feedback/context` endpoint'i
üzerinden LLM sistem prompt'una ya da tool-result payload'una enjekte edilebilir.

---

## Hızlı Başlangıç

```bash
# 1. Bağımlılıkları kur
pip install -e ".[dev]"

# 2. Ortam değişkenlerini ayarla
cp .env.example .env
# .env dosyasını OpenStack bilgilerinizle doldurun

# 3a. MCP Server (SSE modu)
openstack-mcp-server

# 3b. MCP Proxy (ayrı terminal)
openstack-mcp-proxy

# 3c. Docker ile tek seferde
docker compose up
```

---

## Tool Yapısı

| Prefix | Tür       | Açıklama                          |
|--------|-----------|-----------------------------------|
| `get_` | Read-only | OpenStack'ten bilgi toplar, cache'lenir |
| `set_` | Write     | Operasyonel işlem yapar, audit kaydı tutulur |

### Mevcut Tools

| Tool | Tür | Açıklama |
|------|-----|----------|
| `get_projects` | GET | Tüm Keystone projelerini listeler |
| `get_instances` | GET | Tüm projelerdeki tüm instance'ları listeler |
| `get_instance_detail` | GET | Tek instance'ın detaylı bilgisi |
| `set_instance_delete` | SET | Instance siler (`confirm=true` gerekir) |
| `set_instance_action` | SET | start / stop / reboot / pause / suspend |

---

## Yeni Tool Ekleme

1. `mcp_server/tools/<servis>/` altında dosya oluştur:

```python
# mcp_server/tools/compute/get_hypervisors.py
from mcp_server.tools.base import BaseTool, ToolResult

class GetHypervisors(BaseTool):
    NAME = "get_hypervisors"
    DESCRIPTION = "List all Nova hypervisors."
    INPUT_SCHEMA = {"type": "object", "properties": {}, "required": []}

    async def _run(self, **kwargs) -> ToolResult:
        conn = await get_admin_connection()
        hvs = await list_sdk(conn.compute.hypervisors(details=True))
        return ToolResult(success=True, data=[...])
```

2. `mcp_server/registry.py` içindeki `TOOL_MANIFEST`'e ekle:

```python
"get_hypervisors": "mcp_server.tools.compute.get_hypervisors:GetHypervisors",
```

Server'ı yeniden başlatmana gerek yok — lazy import sayesinde tool ilk
çağrıldığında yüklenir.

---

## LLM Feedback

Her tool çalıştırıldığında bir `FeedbackEvent` otomatik emit edilir.

```bash
# SSE stream (real-time)
curl http://localhost:8080/feedback

# Son 10 event (polling)
curl http://localhost:8080/feedback/recent?n=10

# LLM-ready context string
curl http://localhost:8080/feedback/context?n=10
```

Örnek context çıktısı (LLM sistem prompt'una eklenebilir):

```
=== Recent OpenStack Operations ===
[2024-01-15T10:30:00Z] Tool: get_instances (GET)
Status: SUCCESS  |  Duration: 312.4ms
Inputs: {'limit': 500}
Output: [{"id": "abc-123", "name": "web-01", "status": "ACTIVE", ...}]
────────────────────────────────────────
[2024-01-15T10:29:45Z] Tool: set_instance_delete (SET)
Status: SUCCESS  |  Duration: 890.2ms
Inputs: {'instance_id': 'xyz-789', 'confirm': True}
Output: {"deleted": {"name": "old-vm"}, "message": "..."}
```

---

## Mimari Kararlar

| Karar | Neden |
|-------|-------|
| **Lazy imports** | Server hızlı başlar; SDK sadece ilk tool çağrısında yüklenir |
| **AsyncTTLCache** | OpenStack API çağrılarını azaltır; GET tool'lar otomatik cache'lenir |
| **FeedbackBus** | LLM'e operasyon geçmişi verir; SSE ile real-time, polling ile batch |
| **AuditLog** | SET operasyonları için silinmez kayıt; compliance/debug için |
| **UpstreamRouter** | Proxy katmanı birden fazla MCP server'ı yönetebilir |
| **confirm=true gate** | Yıkıcı SET operasyonları için LLM'in açık onay vermesi zorunlu |
| **asyncio.to_thread** | openstacksdk sync — event loop'u bloke etmeden çalıştırılır |

---

## Testler

```bash
pytest tests/ -v
pytest tests/ --cov=core --cov=mcp_server --cov-report=term-missing
```

---

## Ortam Değişkenleri

Tüm değişkenler için `.env.example` dosyasına bakın.

Kritik olanlar:

| Değişken | Açıklama |
|----------|----------|
| `OS_AUTH_URL` | Keystone endpoint |
| `OS_USERNAME` / `OS_PASSWORD` | Admin kullanıcı |
| `CACHE_TTL` | Saniye cinsinden cache süresi (varsayılan: 300) |
| `MCP_TRANSPORT` | `sse` veya `stdio` |
| `PROXY_UPSTREAM_URLS` | Virgülle ayrılmış MCP server URL'leri |
