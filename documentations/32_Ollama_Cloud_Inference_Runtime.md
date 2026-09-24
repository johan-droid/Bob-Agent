---
title: Ollama Cloud-First Inference Runtime
type: architecture
project: Agent System
status: implemented
updated: 2026-09-24
up: "[[00_Index]]"
---

# Ollama Cloud-First Inference Runtime

> **Code:** `agent-system/backend/src/agent_system/services/inference_runtime.py`
> (`internal`), config in `config.py`, persistence in `infra/models.py`,
> checkpoints in `services/checkpoints.py`, tests in
> `tests/unit/test_ollama_inference_runtime.py`.

Bob is **one continuous agent on one selected model** — not a free-API
roulette. Ollama Cloud is the primary inference platform; Groq, Gemini and
OpenRouter remain available but are **emergency fallbacks only**.

```
Telegram
   ↓
Agent Session
   ↓
Task Classification
   ↓
Ollama Cloud Model Selection
   ↓
Session Model Lock
   ↓
Agent Loop  (tools · memory · context · checkpoints)
   ↓
Response
```

## 1. Decision layer, not a new architecture

The runtime does **not** replace the existing provider seam. It drives the
same `ModelRouter` / `ProviderAdapter` interface already in use
(`invoke` / `invoke_streaming`) and reuses the existing capability catalog
(`llm_catalog`), error taxonomy (`llm_contract`), health tracker
(`provider_health`) and provider specs (`providers`). It owns exactly one
thing that was previously scattered: **which model a session uses, when to
retry it, and when to leave Ollama Cloud.**

`services/llm_router.py` keeps its capability *ranking* primitives for
introspection; it is no longer the production selection policy.

Production call sites:

- `agents/react_agent.py::llm_react_handler` — the agent task path (tools,
  memory, checkpoints).
- `services/gateway.py` — the Telegram CHAT fast path (chat-scoped lock).

## 2. Model roles

Roles are configurable; no single hard-coded model serves everything.

| Role | Env var | Used for |
|---|---|---|
| `GENERAL` | `OLLAMA_GENERAL_MODEL` | default fallback role |
| `FAST` | `OLLAMA_FAST_MODEL` | chat, simple transformations |
| `CODING` | `OLLAMA_CODING_MODEL` | coding, repository work |
| `REASONING` | `OLLAMA_REASONING_MODEL` | analysis, trade-offs, design |
| `LONG_CONTEXT` | `OLLAMA_LONG_CONTEXT_MODEL` | whole-file / transcript tasks |
| `TOOL_USE` | `OLLAMA_TOOL_MODEL` | tool-heavy and research tasks |

An **empty** role is *discovered* from the capability catalog rather than
invented. A configured model the catalog does not know, or one that cannot
satisfy the task's required capabilities, is skipped and reported — never
silently replaced by an incompatible model.

## 3. Task classification (deterministic)

`classify_task(text)` maps a request to a `TaskFamily` with fixed precedence:
long-context signals → repository work → simple transformation → reasoning →
anchored research openers → the shared cheap classifier
(`services/classifier.py`). **No LLM is ever asked to classify another LLM's
task.**

Families: `CHAT`, `CODING`, `REPOSITORY_WORK`, `REASONING`, `RESEARCH`,
`TOOL_HEAVY`, `LONG_CONTEXT`, `SIMPLE_TRANSFORMATION`.

## 4. Selection pipeline

1. **Effective primary** — offline providers (`echo`/`none`) stay
   authoritative; if `OLLAMA_CLOUD_API_KEY` is unset the deployment keeps
   running on its configured provider (reported, not hidden).
2. **Session lock** — an existing lock for the session is reused verbatim
   when the model is still available and capability-compatible.
3. **Role model** — configured, else discovered.
4. **Capability compatibility** — a model that cannot call tools is never
   selected for a tool-requiring task (`TOOL_UNSUPPORTED` re-selects a
   compatible Ollama Cloud model).
5. **Ordered chain** — the primary Ollama model, then compatible Ollama
   Cloud alternates, then emergency providers. Only compatible models appear.

Selection is inspectable: every decision carries a human-readable `reason`
and the ordered candidate chain (`ModelSelection.to_json()`).

## 5. Session model lock

`SessionModelLock` pins `(provider, model)` to a session (task id fallback for
session-less callers). Every later inference call inside that session reuses
the model **without re-selecting**. The lock is durable too: one
`inference_model_locks` row per session (upsert), so a restart resumes the
same model. A lock is released only when the model is genuinely unavailable
(hard failure) or capability-incompatible.

## 6. Retry, then emergency fallback

For the locked model:

```
temporary error → bounded retry (exponential, capped, honours Retry-After)
                → persistent failure
                → capability failure?  → another compatible Ollama model
                → otherwise            → checkpoint → emergency provider
```

| Error kind | Retried? | Recovery |
|---|---|---|
| `RATE_LIMITED` | yes (4× backoff, honours `Retry-After`) | same model |
| `TIMEOUT` | yes | same model |
| `SERVER_ERROR` | yes | same model |
| `PROVIDER_UNAVAILABLE` | yes | same model |
| `UNKNOWN` | yes | same model |
| `AUTH_FAILED` | no | model/provider dropped |
| `MODEL_NOT_FOUND` | no | another compatible Ollama model |
| `TOOL_UNSUPPORTED` | no | another compatible Ollama model |
| `QUOTA_EXHAUSTED` | no | another compatible Ollama model |
| `CONTEXT_TOO_LARGE` | no | compacted context / another model |
| `INVALID_REQUEST` | no | next candidate (never a 3× retry storm) |

- Never a busy loop; a slow-but-successful request is never abandoned.
- Rate-limit backoff is 4× the base delay and is clamped to the configured
  cap, also honouring the provider's own `Retry-After` cooldown.

### Plan / access refusals (`QUOTA_EXHAUSTED`)

Ollama Cloud answers **HTTP 402 `Payment Required`** for models outside the
account's plan ("this model is not included in your free usage"). Since the
catalog only registers free-plan models, this in practice only fires when a
role is *explicitly configured* to a paid model — and that is exactly the
desired behaviour. It is an *access* fact, not a blip:

- it is **never retried** — one call decides it;
- the model is marked `UNAVAILABLE` and skipped by every later selection, so
  the system **learns** it once instead of once per task;
- the runtime switches to another compatible **Ollama Cloud** model first and
  only then considers the emergency layer;
- a real success (or a process restart) clears the mark — a model is never
  permanently blacklisted, and a fresh deployment re-probes it.

## 7. Emergency fallback contract

- Fallback is **task-scoped**: it never becomes sticky. The next task
  re-evaluates Ollama Cloud.
- The task keeps its ids, tool state, memory and context — only the model
  moves.
- Telegram receives one concise, user-safe status (`FALLBACK_NOTICE`):

  ```
  ⚠️ Ollama Cloud is temporarily unavailable.
  I'm continuing with a compatible fallback model.
  ```

  Raw provider errors, internal ids and credentials are never surfaced.

## 8. Checkpointing

Before continuing elsewhere, a checkpoint is written
(`services/checkpoints.py`, table `inference_checkpoints`): task state,
context reference, completed steps, pending step, tool results, active
provider/model, execution metadata.

- **Idempotent** — one row per task, upserted.
- **Secret-free** — the payload passes through `redact_dict` (key markers and
  value patterns) before storage; API keys, authorization headers and
  credentials never reach the database.
- **Never fatal** — a storage failure is logged and ignored.
- Marked `resumed` once the task continues on the fallback provider.

## 9. Health

`InferenceHealthTracker` derives `HEALTHY` / `DEGRADED` /
`TEMPORARILY_UNAVAILABLE` / `UNAVAILABLE` from **real inference requests
only** — there is no periodic health polling, so an idle system makes no
extra API calls. Tracked per provider/model: last success/failure, timeouts,
consecutive failures, last error.

`AUTH_FAILED`, `MODEL_NOT_FOUND` and `QUOTA_EXHAUSTED` mark a model
`UNAVAILABLE` (hard facts about the model or the account's access to it);
repeated transient failures mark it `TEMPORARILY_UNAVAILABLE`. Role
resolution and the candidate chain both skip `UNAVAILABLE` models, which is
what makes a plan refusal self-healing.

## 10. Configuration

```dotenv
PRIMARY_PROVIDER=ollama_cloud

OLLAMA_GENERAL_MODEL=
OLLAMA_FAST_MODEL=
OLLAMA_CODING_MODEL=
OLLAMA_REASONING_MODEL=
OLLAMA_LONG_CONTEXT_MODEL=
OLLAMA_TOOL_MODEL=

OLLAMA_STREAMING=true
OLLAMA_RETRY_ATTEMPTS=3
OLLAMA_RETRY_BASE_DELAY=1.0
OLLAMA_RETRY_MAX_DELAY=20.0

EMERGENCY_FALLBACK_ENABLED=true
FALLBACK_PROVIDERS=groq,gemini,openrouter
TASK_CHECKPOINTING=true
```

`LLM_PROVIDER_ORDER` / `LLM_MAX_FALLBACK_ATTEMPTS` still exist for the legacy
capability-ranking paths but no longer decide the production model. No paid
API is required: Ollama Cloud is registered as a free tier.

### Free-plan model set (verified 2026-09-24)

Ollama's credit-based pricing (Aug 2026) restricts the free plan to a set of
**starter models** plus a small monthly credit pool; every other cloud model
answers **HTTP 402 `"this model is not included in your free usage"`**. The
catalog therefore registers **only** models verified usable (HTTP 200, tool
calling verified where noted) against the live API with the deployment's own
key:

| Model | Notes |
|---|---|
| `gpt-oss:20b` | fast, tool-capable |
| `gpt-oss:120b` | reasoning, tool-capable |
| `gemma4:31b` | 256K context |
| `nemotron-3-nano:30b` | small/fast |
| `nemotron-3-super` | |
| `nemotron-3-ultra` | reasoning, tool calling verified |

Paid models (`qwen3.5:397b`, `deepseek-v4.1-flash`, `glm-*`, `kimi-*`,
`minimax-*`, `deepseek-v4-*`, `mistral-large-3`) and retired ids
(`qwen2.5-coder`, `llama3.3` — both HTTP 404) are deliberately **not**
registered: selecting one can only produce a 402. A model that becomes
reachable through a plan upgrade is added here after a live verification, not
by hand-editing the catalog in place.

To re-verify the access matrix:

```bash
# Status codes only; never log the key.
curl -s -o /dev/null -w '%{http_code}' https://ollama.com/v1/chat/completions \
  -H "Authorization: Bearer $OLLAMA_CLOUD_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"<id>","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'
# 200 = on the plan; 402 = paid-only; 404 = retired/unknown.
```

## 10b. Diagnosable provider errors

A bare `HTTPStatusError: Client error '400 Bad Request'` says nothing. When a
provider returns an error body, `ModelRouter` appends a **redacted** excerpt
of it to the recorded error (`| provider_body: …`), so it reaches the log line
and the `model_calls.error_json` row. That is how a wrong model id or an
unsupported parameter becomes visible — without ever storing a credential.

## 11. Events & Telegram

New canonical events (`domain/events.py`):

| Event | Meaning |
|---|---|
| `inference.model_selected` | the one model decision for this task |
| `inference.model_ok` | the call succeeded (with `fallback` flag) |
| `inference.model_switched` | moved to another compatible Ollama model |
| `inference.retry` | bounded retry of the same model (with delay) |
| `inference.fallback_activated` | the emergency layer engaged |
| `inference.fallback_notice` | user-facing notice text |
| `inference.exhausted` | every candidate failed |

The Telegram presenter renders the compact status
`⚙️ Ollama Cloud · <model>` on its single progress message and posts the
fallback notice once (deduplicated, never repeated).

## 12. Diagnostics

`validate_configuration(settings)` reports credentials, resolved and disabled
roles, unknown configured models and their capabilities. It is logged at
startup (`api/main.py`) and never fails the application because one optional
role is unavailable. An unavailable role degrades to a compatible one.

## 13. Tests

`tests/unit/test_ollama_inference_runtime.py` (mocked providers, no paid API):
Ollama success/streaming/tool calls/malformed/timeout/unavailable-model,
error taxonomy, session lock (incl. reconfiguration and genuine
unavailability), retry/backoff/`Retry-After`/exhaustion, fallback with
checkpoint + secret redaction + idempotency + context preservation, capability
routing, recovery/non-stickiness, and Telegram progress/notice behaviour.
