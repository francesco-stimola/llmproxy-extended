# PII Detection & Masking

llmproxy-extended uses **OpenAI Privacy Filter** — a transformer NER model fine-tuned specifically on PII data — as the primary detection engine, running via ONNX Runtime entirely offline. The original Presidio engine is kept in the pipeline but disabled by default.

## Detection Modes

### ONNX Privacy Filter (default, priority 19)

The `ONNX PII Masker` plugin runs at PRE_FLIGHT priority 19 using the `openai/privacy-filter` model. It is a fine-tuned token classification transformer that detects PII in context — including names embedded in natural language, not just structured patterns.

```bash
# Download model (one-time, ~1.5 GB for int8 variant)
huggingface-cli download openai/privacy-filter \
  onnx/model_quantized.onnx onnx/model_quantized.onnx_data \
  tokenizer.json tokenizer_config.json config.json special_tokens_map.json
```

Detected categories:

| Category | Placeholder format |
|---|---|
| Names, people | `[PRIVATE_PERSON_1]` |
| Email addresses | `[PRIVATE_EMAIL_1]` |
| Phone numbers | `[PRIVATE_PHONE_1]` |
| Street addresses | `[PRIVATE_ADDRESS_1]` |
| URLs (in PII context) | `[PRIVATE_URL_1]` |
| Dates (birth dates, etc.) | `[PRIVATE_DATE_1]` |
| IBANs, VAT codes, fiscal IDs | `[ACCOUNT_NUMBER_1]` |
| Passwords, API keys, tokens | `[SECRET_1]` |

The same value always gets the same placeholder within a single request (consistency via reverse index). Counters increment per category: `[PRIVATE_PERSON_1]`, `[PRIVATE_PERSON_2]`, etc.

Model variants available (configure in `plugins/manifest.yaml`):

| Variant | Size | Notes |
|---|---|---|
| `int8` | 1.5 GB | **Default** — DML-safe on Windows (DirectML) |
| `fp16` | ~2.7 GB | Fastest on CPU, split into `_data` file |
| `fp32` | ~5.6 GB | Full precision baseline |
| `q4`, `q4f16` | ~0.9 GB | Smaller but not DML-safe |

> **Windows / DirectML note:** Only `int8` is verified to produce output identical to CPU on DirectML (AMD/Intel iGPU). Other variants may miss PII due to GPU↔CPU round-trip divergence in ONNX's MoE/QMoE operators. The plugin automatically falls back to CPU for unsafe variants.

### Presidio NLP (disabled, priority 20)

Microsoft Presidio with spaCy `en_core_web_sm` is still registered in the pipeline at priority 20 but disabled. To revert to Presidio instead of ONNX:

```yaml
# plugins/manifest.yaml
- name: "ONNX PII Masker"
  enabled: false   # disable ONNX

- name: "PII Neural Masker"
  enabled: true    # re-enable Presidio
```

Install Presidio separately (not in requirements.txt by default):

```bash
pip install presidio-analyzer presidio-anonymizer
```

### Regex Safety Net (always active)

`SecurityShield` always runs regex patterns as a safety net for structured PII, independent of which plugin is active:

| Pattern | Example |
|---|---|
| Email | `user@example.com` |
| Phone (US + intl) | `+1-555-0123` |
| SSN | `123-45-6789` |
| Credit card (Luhn-checked) | `4111-1111-1111-1111` |
| IBAN | `DE89370400440532013000` |
| IP address | `192.168.1.1` |
| API key patterns | `sk-...`, `Bearer ...` |

## Vault Tokenization

PII is replaced with vault tokens, not deleted:

```
Input:  "Contact mario.rossi@example.com or call Mario Rossi"
Output: "Contact [PRIVATE_EMAIL_1] or call [PRIVATE_PERSON_1]"
```

Originals are stored in `pii_vault` — a `TTLCache` (10 000 entries, 1 h TTL) in `core/security.py`. Responses are automatically **demasked** in Ring 4 POST_FLIGHT (`shield_sanitizer` → `demask_pii()`) before returning to the client.

## How It Works

```
Ring 2 PRE_FLIGHT (priority 19)
  ONNX PII Masker → mask_text() → updates pii_vault
              ↓
  Request sent to LLM provider (only sees placeholders)
              ↓
Ring 4 POST_FLIGHT
  demask_pii(response) → restores originals from vault
              ↓
  Client receives original PII values
```

## Pipeline Position

| Priority | Plugin | Status |
|---|---|---|
| 19 | ONNX PII Masker (OpenAI Privacy Filter) | **Active** |
| 20 | PII Neural Masker (Presidio) | Disabled |

The plugin processes **all messages** in the request body — user prompts, system messages, tool results, file contents, MCP outputs — anything in the `messages` array.

## Memory Profile & Chunked Inference

ONNX Runtime's self-attention is **O(n²)** in memory: the attention matrix for a sequence of length *n* across *h* heads requires `h × n² × 4 bytes`. For the `openai/privacy-filter` model (12 heads), processing a full Claude Code context (~10 000–15 000 tokens) without chunking would allocate:

| Sequence length | Attention memory | + Model weights | Peak RSS |
|---|---|---|---|
| 512 tokens | ~12 MB | +1.6 GB | ~1.6 GB |
| 4 096 tokens | ~768 MB | +1.6 GB | ~2.4 GB |
| 10 720 tokens | ~5.5 GB | +1.6 GB | **~7.3 GB** |

To keep RAM flat, `OnnxPiiMasker` processes the token stream in **512-token windows** (constant `_CHUNK_SIZE = 512`). Each ONNX call sees at most 512 tokens → ~12 MB attention memory regardless of total context size. PII spans are at most a few words, so no entity ever straddles a chunk boundary.

The tokenizer (`tokenizers` Rust library) encodes the full text once and returns per-token character offsets relative to the original string, so entity positions are always correct without any offset re-mapping per chunk.

**Observed RAM (int8 variant, CPU):** ~1.77 GB total process RSS (model memory-mapped, not counted in Task Manager's "private working set" column).

> **Why `tokenizers` instead of `transformers`?**  
> `transformers.__init__` runs backend detection that triggers `import torch` as a side-effect, adding 2–3 GB of RAM just for the library. Using `tokenizers` (the Rust BPE library) directly avoids the entire `torch` import path. The ONNX session handles inference; `tokenizers` handles tokenization. `torch` is never loaded.

## Configuration

```yaml
# plugins/manifest.yaml
- name: "ONNX PII Masker"
  enabled: true
  config:
    model_id: "openai/privacy-filter"
    variant: "int8"      # int8 | fp16 | fp32 | q4 | q4f16
    backend: "auto"      # auto | cpu | directml
```

```yaml
# Audit log PII masking (always active, independent of plugin)
logging:
  audit_trail:
    enabled: true
    mask_pii: true
```

Force CPU inference (disables DirectML regardless of config):

```bash
PRIVACY_TOOL_FORCE_CPU=1 python main.py
```
