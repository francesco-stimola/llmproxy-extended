# Claude Code Integration Guide

This document describes how to route **Claude Code** (VS Code extension or CLI) transparently through llmproxy-extended, so every request passes through the full plugin pipeline (PII masking, budget guard, headroom compression, agentic loop breaker) without requiring any API key on the proxy itself.

## How it works

Claude Code uses the Anthropic SDK internally, which respects `ANTHROPIC_BASE_URL` to redirect requests to a custom endpoint. The proxy exposes `/v1/messages` in Anthropic wire format and forwards authenticated requests to `api.anthropic.com` after running all plugins.

**Auth passthrough**: Claude Code authenticates via OAuth (`sk-ant-oat01-*` tokens). The proxy forwards the `Authorization: Bearer` header as-is — no key needs to be configured on the proxy side.

## Configuration

Create `.claude/settings.json` in the project root (next to `config.yaml`):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:8090"
  }
}
```

> **Note**: the URL must NOT include `/v1`. The Anthropic SDK appends `/v1/messages` automatically; including `/v1` in the base URL produces a double-path (`/v1/v1/messages`) and a 404.

Open (or reload) the VS Code workspace from that directory. Claude Code picks up the setting and routes all requests through the proxy.

## Plugin pipeline on each request

```
Claude Code → proxy :8090/v1/messages
  ↓ INGRESS          (auth check — disabled in personal mode)
  ↓ PRE_FLIGHT
      PII Neural Masker   masks names, dates, URLs, etc. before Anthropic sees them
      Smart Budget Guard  estimates cost, blocks if session/team budget exceeded
      Headroom Compressor compresses long context windows (Kompress ONNX)
      Agentic Loop Breaker blocks repeated identical requests (retry storms)
  ↓ forward → api.anthropic.com/v1/messages  (with Bearer token)
  ↓ POST_FLIGHT       (demasking, sanitization)
  ↓ response → Claude Code
```

## Verified test results

Tests were run on 2026-07-01 with `claude-sonnet-5` via the VS Code extension, proxy on `localhost:8090`.

| # | Scenario | What was verified | Result |
|---|----------|-------------------|--------|
| 1 | Direct PII in prompt | Sent text with name, DOB, email, phone | `[PRIVATE_DATE, PRIVATE_PERSON, PRIVATE_URL]` masked before Anthropic |
| 1 | Context compression | Conversation grew to ~10 k tokens | `10832 → 9441 tokens (−12.8%)` via Kompress |
| 2 | PII in tool output | Bash tool echoed CF + IBAN + name; Claude summarised | `[PRIVATE_DATE, PRIVATE_PERSON, PRIVATE_URL]` masked in follow-up request |
| 2 | Agentic Loop Breaker | Error caused 4 identical retries | Blocked at retry 4 with `429 Too Many Requests` |
| 3 | Compression on repetitive text | Pasted ~800-word repetitive legal contract | `11157 → 9775 tokens (−12.4%)` saved |
| 3 | PII masking on large context | Same session as test 3 | Masking confirmed on every request throughout |

**Pending tests** (not yet run):
- PII masking when Claude Code reads a file containing sensitive data via the Read tool
- PII masking through an MCP tool call

## Known behaviors

- **Cold-start latency**: the first request after proxy startup (or after a daily budget reset) is slower because the ONNX PII model and Kompress model load in the background. Subsequent requests use cached models.
- **Compression latency**: Kompress runs on CPU (ONNX, pure-Python backend on Windows). Compressing ~10 k-token contexts takes 15–17 s on the first pass; the content router caches results so re-runs of the same content are instant.
- **Parallel retry noise**: when compression is slow, Claude Code may send parallel retry requests. The Agentic Loop Breaker catches these and returns `429`; the primary request still succeeds.
- **PII categories detected**: the ONNX NER model reliably detects `PRIVATE_PERSON`, `PRIVATE_DATE`, `PRIVATE_URL`. Italian-specific identifiers (CF, IBAN) are not yet a dedicated category in the default model.

## Starting the proxy

```powershell
cd c:\Lavoro\Repository\Git\Personale\llmproxy-extended
.venv\Scripts\python.exe main.py
```

Stop with `Ctrl+C` — the proxy shuts down cleanly (background tasks cancelled, HTTP session closed).
