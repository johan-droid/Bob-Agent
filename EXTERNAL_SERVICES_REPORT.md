# Bob Agent — External Services & Drivers Implementation Report

## A. Dependency Report

| Existing Dependency | New Dependency | Why Required | Optional/Mandatory |
| --- | --- | --- | --- |
| `fastapi` | None | Web API framework | Mandatory |
| `sqlalchemy`, `psycopg2-binary` | None | PostgreSQL transactional database | Mandatory |
| `redis`, `rq` | None | Queueing, task locks, temporary state | Mandatory (Optional runtime if `CLOUD_INLINE_RUN=true`) |
| `httpx` | None | HTTP client for Telegram, LLM providers, OpenConnector | Mandatory |
| `pydantic`, `pydantic-settings` | None | Configuration & data validation | Mandatory |
| None | `pymongo` | Optional MongoDB Atlas datastore adapter | **Optional** (Soft import) |
| None | `boto3` | Optional S3-compatible object storage provider | **Optional** (Soft import) |

**Dependency Hygiene Verification:**
- Zero mandatory new dependencies added.
- `uv pip check` passed with zero conflicts.
- Heroku Python buildpack compatibility preserved without Node buildpack requirements.

---

## B. External Service Matrix

| Service | Provider | Driver / Adapter | Configuration | Health Check | Fallback | Free Tier Status |
| --- | --- | --- | --- | --- | --- | --- |
| Primary Database | PostgreSQL / SQLite | `sqlalchemy` + `psycopg2-binary` | `DATABASE_URL` | `SELECT 1` query | Fallback to SQLite locally | Free tier / Self-hosted / Open source |
| Queue / Locks | Redis | `redis` / `rq` | `REDIS_URL` | `PING` command | `CLOUD_INLINE_RUN=true` inline execution | Free tier / Self-hosted / Open source |
| Secondary Storage | MongoDB Atlas | `pymongo` | `MONGODB_ENABLED`, `MONGODB_URI` | `ping` admin command | Disabled mode, Bob core continues | Free tier available |
| Durable Object Storage | Local / S3 | `LocalStorageProvider` / `boto3` | `STORAGE_PROVIDER`, `S3_BUCKET_NAME` | File write / HeadBucket | Local filesystem fallback | Local free / S3 free tier |
| LLM Router | Groq, Gemini, OpenRouter, OpenAI, Anthropic | `httpx` native adapters | `GROQ_API_KEY`, `GEMINI_API_KEY`, etc. | Provider health tracker / model router probe | Groq → Gemini → OpenRouter → Next configured | Free tiers & zero-cost models supported |
| Chat Gateway | Telegram | `httpx` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS` | Telegram `getMe` API call | Local CLI / REST API mode | Free tier / Zero-cost API |
| SaaS Gateway | OpenConnector | `httpx` / MCP HTTP client | `OPENCONNECTOR_BASE_URL` | `GET /health` endpoint | Disabled mode, tools hidden | Self-hosted / Open source |
| Search Capability | DuckDuckGo | `DuckDuckGoSearchProvider` (`httpx` + `bs4`) | None | Test query fetch | Degraded mode / fetch direct URL | Zero-cost / Free |

---

## C. Architecture Report

The architecture establishes a strict unidirectional boundary where provider-specific details do not leak into Bob Agent core logic:

```
External Service (PostgreSQL, Redis, MongoDB, S3, LLMs, Telegram, MCP, DDG)
      ↓
Driver / SDK (psycopg2, redis, pymongo, boto3, httpx)
      ↓
Bob Adapter (PostgresService, RedisService, MongoDBAdapter, LocalStorageProvider/S3, LLMProviderService, TelegramGatewayService, DuckDuckGoSearchProvider)
      ↓
Bob Interface (ExternalService, StorageProvider, SearchProvider, HealthRegistry)
      ↓
Agent Core / Gateway / Swarm / API Endpoints
```

**Key Isolation Principles:**
1. **No direct SDK leakage:** Downstream code interacts with `ExternalService`, `StorageProvider`, or `SearchProvider`.
2. **Failure Isolation:** Each adapter handles timeouts, connection errors, and rate limits gracefully. A failure in MongoDB, Redis, or an MCP server does not bring down the primary Bob runtime.
3. **Configuration Centralization:** All configuration flows through `agent_system.config.Settings`.

---

## D. Test Report

**Tests Added & Executed:**
- `tests/unit/test_external_service_base.py`: Secret redaction, health status data structures, error normalization, retry helper.
- `tests/unit/test_postgres_service.py`: PostgreSQL health checks, connection error handling, URL normalization.
- `tests/unit/test_mongodb_adapter.py`: MongoDB disabled mode, unconfigured state, soft pymongo missing, mock CRUD operations, auth failure redaction.
- `tests/unit/test_redis_service.py`: Redis health checks, task locks, deduplication, caching, fallback isolation.
- `tests/unit/test_storage_service.py`: Local storage upload/download/delete, size limit enforcement, path traversal protection, mock S3 operations.
- `tests/unit/test_llm_service_adapter.py`: LLM provider health tracker integration, secret redaction.
- `tests/unit/test_telegram_service_adapter.py`: Telegram gateway health check, allowed chat IDs, token redaction.
- `tests/unit/test_mcp_openconnector_adapters.py`: MCP server parsing and OpenConnector health check.
- `tests/unit/test_search_provider.py`: DuckDuckGo search HTML parsing, link unwrapping, deduplication, and error handling.
- `tests/unit/test_health_registry.py`: Universal health registry check_all concurrent execution.

**Skipped Tests & Reasons:**
- Real MongoDB integration tests skipped when `pymongo` is not installed (tested via mocks).
- Real S3 integration tests skipped when `boto3` is not installed (tested via mocks).

---

## E. Deployment Report

- **Build Verification:** Clean checkout and dependency resolution via `uv pip check` passed.
- **Heroku Procfile Compatibility:**
  - `web: PYTHONPATH=src python -m uvicorn agent_system.api.main:app --host 0.0.0.0 --port ${PORT:-8000}`
  - `worker: PYTHONPATH=src python -m agent_system.worker`
  - `release: PYTHONPATH=src python -m alembic upgrade head`
- **Execution Modes Verified:**
  - `CLOUD_INLINE_RUN=true` verified (inline execution without requiring Redis).
  - Worker execution mode verified.
- **Health & Readiness Endpoints:** `/health` and `/ready` endpoints updated to use `HealthRegistry` with bounded timeouts.

---

## F. Security Report

1. **Secret Protection:**
   - Database passwords, API keys, Telegram bot tokens, and AWS secrets are automatically redacted in health outputs, error messages, and logs via `redact_secrets()`.
2. **Path Traversal Protection:**
   - `LocalStorageProvider` enforces safe path resolution restricting all read/write operations to `STORAGE_LOCAL_ROOT`.
3. **Resource Limits:**
   - Object storage enforces `MAX_FILE_SIZE_MB`, `MAX_OUTPUT_SIZE_MB`, and `MAX_WORKSPACE_SIZE_MB`.
4. **Approval & Permission Gates:**
   - OpenConnector, A2A, and tool permissions remain gated through the central `PermissionGate`.
