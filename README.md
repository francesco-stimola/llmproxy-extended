# llmproxy-extended

Fork of [fabriziosalmi/llmproxy](https://github.com/fabriziosalmi/llmproxy) extended with ONNX-based PII anonymization (OpenAI Privacy Filter), context compression via Headroom, and full traffic coverage: prompts, tool results, file reads and MCP outputs.

![Python](https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi&logoColor=white)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

---

## What's new in this fork

The original llmproxy uses Microsoft Presidio (spaCy `en_core_web_sm`) for PII detection. This fork replaces it with the **OpenAI Privacy Filter** — a transformer NER model fine-tuned specifically on PII data — and adds optional context compression via Headroom.

| Feature | Original llmproxy | llmproxy-extended |
|---|---|---|
| PII detection | Presidio + spaCy (general-purpose) | OpenAI Privacy Filter NER (fine-tuned, ONNX) |
| Multi-language PII | English only | Multi-language (training data) |
| Context PII (names in sentences) | Weak (`en_core_web_sm`) | Strong (transformer fine-tuned on PII) |
| Structured PII (SSN, IBAN...) | Presidio regex + checksum | Regex safety net in SecurityShield |
| Context compression | None | Headroom `[ml,code]` (opt-in, 60-95% tokens) |
| PII categories | 11 (Presidio) | 8 focused: PERSON, EMAIL, PHONE, ADDRESS, URL, DATE, ACCOUNT, SECRET |
| Traffic covered | All API messages | All API messages (same proxy layer) |

Both approaches cover all traffic that passes through the proxy: user prompts, tool results, file reads, MCP outputs — anything that ends up in the `messages` array before the API call reaches the LLM provider.

---

## Architecture

```
Client Request (Claude Code / any OpenAI-compatible client)
  |
  +-- RateLimitMiddleware
  +-- ByteLevelFirewall (180 signatures, 8 encoding layers)
  +-- CORSMiddleware
  +-- Global Auth (fail-closed)
  +-- SecurityShield (injection scoring, regex PII safety net)
  |
  +-- Ring 1: INGRESS        Auth, Zero-Trust, rate limiting
  +-- Ring 2: PRE-FLIGHT
  |     priority 11  Smart Budget Guard
  |     priority 12  Agentic Loop Breaker
  |     priority 15  Aider Context Minifier
  |     priority 19  ONNX PII Masker        ← [NEW] OpenAI Privacy Filter
  |     priority 20  PII Neural Masker       ← [OFF] Presidio (disabled)
  |     priority 25  Headroom Compressor     ← [NEW] opt-in, after masking
  |     priority 30  WAF Cache Lookup
  +-- Ring 3: ROUTING        Smart router, A/B, QoS
  +-- Upstream Provider      Format translation + fallback chain
  +-- Ring 4: POST-FLIGHT    demask_pii() restores originals, sanitization
  +-- Ring 5: BACKGROUND     Telemetry, audit, shadow traffic
  |
Client Response (PII restored to original values)
```

The Headroom compressor runs **after** PII masking (priority 25 > 19). This means Headroom's local CCR cache only ever sees already-anonymized text — no original PII is compressed or stored by Headroom.

---

## Quick Start

### 1. Clone and set up the virtual environment

The `.venv/` directory is pre-configured inside the project. Create and activate it:

```bash
git clone https://github.com/YOUR_USERNAME/llmproxy-extended && cd llmproxy-extended
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Download the PII model

The proxy uses **OpenAI Privacy Filter** (ONNX, int8 quantized — 1.6 GB). It must be downloaded once before first run:

```bash
# int8 (recommended, 1.5 GB) — split into .onnx (graph) + .onnx_data (weights)
# ONNX Runtime loads both files automatically from the same directory
huggingface-cli download openai/privacy-filter \
  onnx/model_quantized.onnx onnx/model_quantized.onnx_data \
  tokenizer.json tokenizer_config.json config.json special_tokens_map.json
```

Available model variants and their tradeoffs:

| Variant | File | Size | Speed | Notes |
|---|---|---|---|---|
| `int8` | `onnx/model_quantized.onnx` | 1.6 GB | Fast | **Recommended** — DML-safe on Windows |
| `fp16` | `onnx/model_fp16.onnx` | 2.8 GB | Fastest on CPU | ~102 ms avg |
| `fp32` | `onnx/model.onnx` | 5.6 GB | Baseline | Full precision |
| `q4` | `onnx/model_q4.onnx` | 0.9 GB | Slow on CPU | Not DML-safe |
| `q4f16` | `onnx/model_q4f16.onnx` | 0.8 GB | Slow on CPU | Not DML-safe |

> **Windows / DirectML note:** Only `int8` is verified to produce output identical to CPU on DirectML (AMD/Intel iGPU). Other variants have GPU↔CPU round-trip divergence that can miss PII. The plugin automatically refuses DirectML for non-safe variants and falls back to CPU.

Switch variant at any time in `plugins/manifest.yaml` under the ONNX PII Masker config — no reinstall needed.

### 3. Configure your LLM provider

#### Personal / single-user mode (recommended for local use)

The proxy runs on your machine and you are the only user. The ingress auth plugin is
**disabled by default** in `plugins/manifest.yaml`, so your real upstream API key
flows through the proxy transparently — no extra proxy token to manage.

Just tell the proxy where your provider lives. For cloud providers the key is already
in your shell session; for local models declare the URL:

```bash
# .env — cloud provider (key comes from your existing shell environment)
# Nothing extra needed: ANTHROPIC_API_KEY is already set in your Claude Code session.

# .env — local model (Ollama, LM Studio, vLLM …)
LLM_PROXY_ENDPOINT_OLLAMA_URL=http://localhost:11434/v1
LLM_PROXY_ENDPOINT_OLLAMA_MODELS=llama3.2,qwen2.5-coder
```

Point Claude Code at the proxy — that is the only change needed:

```bash
# Shell or Claude Code environment
ANTHROPIC_BASE_URL=http://localhost:8090/v1
# ANTHROPIC_API_KEY stays unchanged — the proxy passes it through to Anthropic.
```

#### Team / multi-user mode

Multiple users or services share the same proxy instance. The upstream key lives only
on the proxy; clients authenticate with short-lived proxy tokens.

Enable ingress auth in `plugins/manifest.yaml`:
```yaml
- name: "Ingress Auth & Zero-Trust"
  enabled: true          # re-enable for team use
```

Then configure `.env`:
```bash
# .env
LLM_PROXY_API_KEYS=sk-proxy-alice,sk-proxy-bob   # one token per client
ANTHROPIC_API_KEY=sk-ant-...                       # real key, only here
```

Clients use a proxy token, not the real key:
```bash
# Claude Code (each user)
ANTHROPIC_BASE_URL=http://localhost:8090/v1
ANTHROPIC_API_KEY=sk-proxy-alice    # proxy token, not the real Anthropic key
```

### 4. Start the proxy

```bash
python main.py
```

The proxy starts on `http://localhost:8090`. Point your client here:

```python
# Personal mode — Anthropic SDK (real key passed through)
from anthropic import Anthropic

client = Anthropic(
    base_url="http://localhost:8090/v1",
    # api_key not set here — comes from ANTHROPIC_API_KEY env var as usual
)
```

```python
# Team mode — OpenAI SDK (proxy token used instead of real key)
from openai import OpenAI

client = OpenAI(
    api_key="sk-proxy-alice",
    base_url="http://localhost:8090/v1",
)
```

---

## PII Masking

The ONNX PII Masker runs on **all messages** in Ring 2 PRE_FLIGHT — before anything reaches the LLM provider. This includes:
- User prompts
- System messages
- Tool results embedded in the conversation
- File contents read by the agent
- MCP server outputs

Detected categories and placeholder format:

| Category | Placeholder |
|---|---|
| Names, people | `[PRIVATE_PERSON_1]` |
| Email addresses | `[PRIVATE_EMAIL_1]` |
| Phone numbers | `[PRIVATE_PHONE_1]` |
| Street addresses | `[PRIVATE_ADDRESS_1]` |
| URLs (in PII context) | `[PRIVATE_URL_1]` |
| Dates (birth dates, etc.) | `[PRIVATE_DATE_1]` |
| IBANs, VAT codes, fiscal IDs | `[ACCOUNT_NUMBER_1]` |
| Passwords, API keys, tokens | `[SECRET_1]` |

The same value always gets the same placeholder within a request (consistency). Originals are stored in the in-memory vault (`pii_vault`, TTL 1h) and restored automatically in Ring 4 POST_FLIGHT before the response reaches the client.

The original Presidio masker (`PII Neural Masker` in `manifest.yaml`) is kept but disabled. To revert to Presidio, set `enabled: true` on it and `enabled: false` on the ONNX masker.

---

## Context Compression (Headroom)

Headroom reduces token count by 60-95% using content-aware compression. It is **disabled by default** and runs after PII masking.

### Why `headroom[ml,code]` and not `headroom[all]`

Headroom ships several independent extras. This fork installs only two:

| Extra | What it adds | Why we include it |
|---|---|---|
| `headroom[ml]` | Kompress-v2-base (ModernBERT, trained on agentic traces) | General text, logs, tool outputs |
| `headroom[code]` | CodeCompressor (tree-sitter AST) for Python/JS/Go/Rust/Java/C/Perl | Source code files fed to the agent |
| `headroom[proxy]` | Standalone HTTP proxy server | ❌ Not needed — we have llmproxy |
| `headroom[mcp]` | MCP server integration | ❌ Not needed — we handle MCP ourselves |
| `headroom[memory]` | Conversation memory management | ❌ Not needed for this use case |
| `headroom[vector]` | Vector store integration | ❌ Not needed for this use case |

Install Headroom when you want compression:

```bash
pip install "headroom[ml,code]"
```

Then enable the plugin in `plugins/manifest.yaml`:

```yaml
- name: "Headroom Compressor"
  enabled: true   # ← change this
```

The `min_tokens_to_compress` config key (default: 200 words) skips compression on short messages where overhead > benefit.

---

## Feature Flags

All extended features are controlled in `plugins/manifest.yaml`:

```yaml
# Enable / disable ONNX PII masking
- name: "ONNX PII Masker"
  enabled: true        # set false to revert to Presidio
  config:
    variant: "int8"    # int8 | fp16 | fp32 | q4 | q4f16
    backend: "auto"    # auto | cpu | directml

# Enable / disable context compression (requires headroom[ml,code])
- name: "Headroom Compressor"
  enabled: false       # set true to activate
  config:
    min_tokens_to_compress: 200
```

No restart needed — plugins support hot-swap via the admin API:

```bash
curl -X POST http://localhost:8090/api/v1/plugins \
  -H "Authorization: Bearer sk-proxy-mykey" \
  -d '{"name": "headroom_compressor", "enabled": true}'
```

---

## Providers

OpenAI, Anthropic, Google (Gemini), Azure OpenAI, Ollama, Groq, Together, Mistral, DeepSeek, xAI (Grok), Perplexity, Fireworks, OpenRouter, SambaNova, plus any OpenAI-compatible endpoint.

---

## Security

| Layer | What it does |
|---|---|
| **ASGI Firewall** | 180 injection signatures across 8 encoding layers. Hot-reloadable. |
| **SecurityShield** | Injection scoring, multi-turn trajectory detection, cross-session ThreatLedger. Regex PII safety net for structured data (IBAN, SSN, cards). |
| **ONNX PII Masker** | Fine-tuned NER transformer on all messages. Vault-based mask/demask roundtrip. |
| **Headroom (opt-in)** | Compresses already-masked text — CCR cache never sees original PII. |
| **Response Sanitization** | Entropy guard, steganography detection, prompt leak detection. |
| **Audit Ledger** | SHA256 hash-chained log. GDPR: right to erasure, DSAR export. |

Auth: API keys, OIDC/JWT (Google, Microsoft, Apple), Tailscale Zero-Trust. RBAC with four roles.

HMAC-SHA256 response signing proves the response was not modified after leaving the proxy.

---

## API

OpenAI-compatible API on port 8090.

| Endpoint | Method | Description |
|---|---|---|
| `/v1/chat/completions` | `POST` | Chat completion (streaming + non-streaming) |
| `/v1/completions` | `POST` | Legacy text completion |
| `/v1/embeddings` | `POST` | Embeddings |
| `/v1/models` | `GET` | Model list (aggregated from all providers) |
| `/health` | `GET` | Liveness probe |
| `/metrics` | `GET` | Prometheus metrics |
| `/api/v1/plugins` | `GET/POST` | Plugin management and hot-swap |
| `/api/v1/audit` | `GET` | Audit log query |
| `/api/v1/gdpr/erase/{subject}` | `POST` | Right to erasure (GDPR Art. 17) |
| `/ui` | `GET` | Security Operations Center UI |

---

## Production Checklist

| Setting | Default | Production |
|---|---|---|
| TLS | Disabled | Enable or use a reverse proxy (Traefik, Caddy, nginx) |
| CORS | `["*"]` | Restrict to your frontend origin(s) |
| Auth | Enabled | Keep enabled, rotate API keys |
| ONNX model | Must download | `huggingface-cli download openai/privacy-filter onnx/model_quantized.onnx ...` |
| Headroom | Disabled | `pip install headroom[ml,code]` + `enabled: true` in manifest |
| DirectML (Windows) | Auto-detected | Only `int8` variant is safe on DirectML |
| tiktoken | Not installed | `pip install tiktoken` for accurate token counting |

---

## Writing Plugins

```python
from core.plugin_sdk import BasePlugin, PluginResponse, PluginHook

class MyPlugin(BasePlugin):
    name = "my_plugin"
    hook = PluginHook.PRE_FLIGHT
    version = "1.0.0"
    author = "you"
    timeout_ms = 100

    async def on_load(self):
        # Called once at startup
        pass

    async def execute(self, ctx):
        body = ctx.body
        # modify body["messages"] here
        return PluginResponse.modify(body=body)
        # or PluginResponse.passthrough()
        # or PluginResponse.block(reason="...")
```

Place the file in `plugins/installed/` and register it in `plugins/manifest.yaml`. WASM plugins (Rust/Go/C) are also supported via Extism.

---

## Observability

- **Prometheus** — 10 metrics (requests, errors, latency, tokens, cost, budget, circuit state). Grafana dashboard in `monitoring/`.
- **OpenTelemetry** — Distributed tracing via OTLP.
- **Sentry** — Exception tracking with PII filtering.
- **Webhooks** — Slack, Teams, Discord (HMAC-SHA256 signed).

---

## Claude Code Integration

Route Claude Code (VS Code extension or CLI) transparently through the proxy with zero key configuration — the OAuth token flows through automatically.

See **[docs/claude-code-integration.md](docs/claude-code-integration.md)** for setup, verified test results, and known behaviors.

---

## License

MIT. See [LICENSE](LICENSE).

Upstream project: [fabriziosalmi/llmproxy](https://github.com/fabriziosalmi/llmproxy) — MIT.
