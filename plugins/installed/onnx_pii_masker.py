"""
ONNX PII Masker — llmproxy-extended
PRE_FLIGHT plugin: replaces the default Presidio masker with the
OpenAI Privacy Filter NER model (fine-tuned transformer, ONNX backend).

Detects 8 PII categories:
  PRIVATE_PERSON, PRIVATE_EMAIL, PRIVATE_PHONE, PRIVATE_ADDRESS,
  PRIVATE_URL, PRIVATE_DATE, ACCOUNT_NUMBER, SECRET

Stores placeholder→value in rotator.security.pii_vault so Ring 4
(shield_sanitizer / demask_pii) restores originals automatically.

Placeholder format: [GROUP_N]  e.g. [PRIVATE_PERSON_1], [PRIVATE_EMAIL_2]
Consistency: within a single request, the same value always gets the same
placeholder (reverse_index). Across requests the vault handles re-mapping.

Requirements: onnxruntime>=1.18.0  tokenizers>=0.19.0  huggingface-hub>=0.20.0
  (does NOT import `transformers` or `torch` — uses the Rust `tokenizers` library
   directly to avoid torch being loaded as a side-effect of transformers.__init__)
Model must be pre-downloaded locally: huggingface-cli download openai/privacy-filter
"""
from typing import Any

from core.plugin_sdk import BasePlugin, PluginHook, PluginResponse
from core.plugin_engine import PluginContext

MODEL_ID = "openai/privacy-filter"

VARIANTS: dict[str, str] = {
    "fp32":  "onnx/model.onnx",
    "fp16":  "onnx/model_fp16.onnx",
    "int8":  "onnx/model_quantized.onnx",
    "q4":    "onnx/model_q4.onnx",
    "q4f16": "onnx/model_q4f16.onnx",
}

# Only int8 produces output identical to CPU when running on DirectML.
# Other variants may miss PII due to GPU/CPU round-trip divergence.
DML_SAFE_VARIANTS: frozenset[str] = frozenset({"int8"})

# Characters that the model sometimes absorbs into a span boundary but are
# never a legitimate edge of a PII value (e.g. "customer=Mario" → strips "=").
_STRIP_PUNCT = frozenset("=<>:;,\"'`()[]{}|!?*")


def _strippable(ch: str) -> bool:
    return ch.isspace() or ch in _STRIP_PUNCT


# Max tokens per ONNX inference call. ONNX Runtime computes full self-attention
# which is O(n²) in memory: at n=10720 → ~5.5 GB for the attention matrix alone.
# Chunking to 512 tokens per call keeps each call under ~12 MB of attention memory.
# PII spans are at most a few words, never spanning a 512-token chunk boundary.
_CHUNK_SIZE = 512


class _OnnxClassifier:
    """Minimal token-classification runner over a raw ONNX Runtime session.

    Uses the `tokenizers` (Rust) library directly instead of `transformers`
    to avoid importing torch as a side-effect of transformers.__init__.

    Long texts are processed in _CHUNK_SIZE-token chunks to avoid O(n²) attention
    memory blowup. Character offsets from the tokenizer map directly back to the
    original text regardless of which chunk a token falls in, so entity positions
    are always correct without any offset re-mapping.

    Token-level entities are stitched into spans by _merge_consecutive,
    mirroring transformers' aggregation_strategy="simple" behaviour.
    """

    def __init__(self, session: Any, tokenizer: Any, id2label: dict[int, str]) -> None:
        self.session = session
        self.tok = tokenizer
        self.id2label = id2label

    def __call__(self, text: str) -> list[dict]:
        import numpy as np

        # tokenizers Rust library: encode() returns an Encoding with
        # .ids, .attention_mask, .offsets (list of (start, end) char tuples).
        # Truncation is disabled here — chunking handles length limits below.
        enc = self.tok.encode(text)
        all_ids = enc.ids
        all_offsets = enc.offsets  # char offsets relative to original text

        ents = []
        # Process in fixed-size chunks. Each chunk is an independent ONNX call,
        # capping peak attention memory at O(_CHUNK_SIZE²) ≈ 12 MB per call.
        for chunk_start in range(0, len(all_ids), _CHUNK_SIZE):
            chunk_ids = all_ids[chunk_start: chunk_start + _CHUNK_SIZE]
            chunk_offsets = all_offsets[chunk_start: chunk_start + _CHUNK_SIZE]

            ids_arr = np.array([chunk_ids], dtype="int64")
            # All tokens in the chunk are real (no padding), so attention mask = 1.
            attn_arr = np.ones((1, len(chunk_ids)), dtype="int64")

            feeds = {"input_ids": ids_arr, "attention_mask": attn_arr}
            logits = self.session.run(["logits"], feeds)[0][0]  # (chunk_len, num_labels)
            label_ids = logits.argmax(-1)
            m = logits.max(-1, keepdims=True)
            ex = np.exp(logits - m)
            probs = ex / ex.sum(-1, keepdims=True)

            for i, lab_id in enumerate(label_ids):
                label = self.id2label[int(lab_id)]
                if label == "O":
                    continue
                start, end = int(chunk_offsets[i][0]), int(chunk_offsets[i][1])
                if start == end:  # special token (BOS/EOS/pad) — offset is (0,0)
                    continue
                group = label.split("-", 1)[-1]  # strip B-/I-/E-/S- prefix
                ents.append({
                    "entity_group": group,
                    "start": start,
                    "end": end,
                    "score": float(probs[i, int(lab_id)]),
                    "word": text[start:end],
                })

        return ents


def _merge_consecutive(entities: list[dict], max_gap: int = 1) -> list[dict]:
    """Stitch adjacent tokens of the same group whose boundaries touch."""
    if not entities:
        return []
    sorted_ents = sorted(entities, key=lambda e: e["start"])
    merged: list[dict] = [dict(sorted_ents[0])]
    for ent in sorted_ents[1:]:
        last = merged[-1]
        same_group = ent.get("entity_group") == last.get("entity_group")
        if same_group and ent["start"] - last["end"] <= max_gap:
            last["end"] = ent["end"]
            last["score"] = max(last.get("score", 0), ent.get("score", 0))
        else:
            merged.append(dict(ent))
    return merged


def _mask_text(
    text: str,
    raw_entities: list[dict],
    vault: Any,
    reverse_index: dict,
    counters: dict,
    debug: bool = False,
) -> str:
    """
    Replace detected entity spans with placeholders.

    vault:         rotator.security.pii_vault — TTLCache(token → original)
    reverse_index: (group, value_lower) → placeholder, shared across all
                   messages in the same request for consistency.
    counters:      group → current highest N, also shared per request.
    debug:         when True, writes placeholder→placeholder (identity) to vault
                   so the de-masker's replace loop is a no-op and the placeholder
                   stays visible in the response. Also overwrites any stale vault
                   entry from a previous request that had the same placeholder key.
    """
    entities = _merge_consecutive(raw_entities, max_gap=1)
    entities = sorted(entities, key=lambda e: e["start"], reverse=True)

    masked = text
    for ent in entities:
        group = ent["entity_group"].upper()
        start, end = ent["start"], ent["end"]

        while start < end and _strippable(text[start]):
            start += 1
        while end > start and _strippable(text[end - 1]):
            end -= 1
        if start >= end:
            continue

        original = text[start:end]
        key = (group, original.strip().lower())
        placeholder = reverse_index.get(key)
        if placeholder is None:
            counters[group] = counters.get(group, 0) + 1
            placeholder = f"[{group}_{counters[group]}]"
            reverse_index[key] = placeholder
            # debug=True: identity mapping so de-masker replace is a no-op.
            # Also overwrites stale vault entries from previous requests that
            # would otherwise restore the real value through the same placeholder.
            vault[placeholder] = placeholder if debug else original

        masked = masked[:start] + placeholder + masked[end:]

    return masked


class OnnxPiiMasker(BasePlugin):
    name = "onnx_pii_masker"
    hook = PluginHook.PRE_FLIGHT
    version = "1.2.2"
    author = "llmproxy-extended"
    description = (
        "PII masking via OpenAI Privacy Filter (ONNX NER). Detects 8 categories: "
        "PRIVATE_PERSON, PRIVATE_EMAIL, PRIVATE_PHONE, PRIVATE_ADDRESS, PRIVATE_URL, "
        "PRIVATE_DATE, ACCOUNT_NUMBER, SECRET. Replaces Presidio default masker. "
        "Uses tokenizers (Rust) directly — does not import transformers or torch."
    )
    # NER inference: 50–300 ms per message on CPU; allow generous headroom.
    timeout_ms = 2000

    def __init__(self, config=None):
        super().__init__(config)
        self._classifier: _OnnxClassifier | None = None
        self._backend: str = "not-loaded"
        # debug_input_only: mask input normally but skip vault population so
        # the de-masker has nothing to restore — the response comes back with
        # raw placeholders ([PRIVATE_PERSON_1] etc.) instead of original values.
        # Useful for verifying what the provider actually received and returned.
        self._debug_input_only: bool = bool(config.get("debug_input_only", False)) if config else False

    async def on_load(self) -> None:
        if self._debug_input_only:
            self.logger.warning(
                "ONNX PII Masker: DEBUG MODE ACTIVE (debug_input_only=true) — "
                "input is masked but responses are NOT de-masked. "
                "Placeholders will appear in LLM output. DO NOT use in production."
            )
        try:
            self._classifier, self._backend = self._build_classifier()
            self.logger.info(f"ONNX PII masker ready — backend={self._backend}")
        except Exception as exc:
            self.logger.error(
                f"Failed to load ONNX model ({exc}). "
                "Ensure the model is downloaded: "
                "huggingface-cli download openai/privacy-filter. "
                "Plugin will passthrough until model is available."
            )

    def _build_classifier(self) -> tuple[_OnnxClassifier, str]:
        import json
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        # Use `tokenizers` (Rust library) directly — avoids importing `transformers`
        # which would trigger torch import via transformers.__init__ backend detection.
        from tokenizers import Tokenizer as _HFTokenizer  # noqa: PLC0415

        model_id: str = self.config.get("model_id", MODEL_ID)
        variant: str = self.config.get("variant", "int8")
        backend_pref: str = self.config.get("backend", "auto")
        force_cpu: bool = bool(self.config.get("force_cpu", False))

        if variant not in VARIANTS:
            self.logger.warning(f"Unknown variant '{variant}', falling back to int8")
            variant = "int8"

        onnx_file = VARIANTS[variant]
        providers = ["CPUExecutionProvider"]
        backend = f"onnx-cpu({variant})"

        want_gpu = not force_cpu and backend_pref in ("auto", "directml")
        if want_gpu and "DmlExecutionProvider" in ort.get_available_providers():
            if variant in DML_SAFE_VARIANTS:
                providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
                backend = f"onnx-directml({variant})"
            else:
                self.logger.warning(
                    f"DirectML disabled for variant '{variant}': GPU output diverges "
                    f"from CPU and can miss PII. Only int8 is DML-safe. Using CPU."
                )

        self.logger.info(f"Loading ONNX model {model_id} [{variant}] via {backend}")
        local_path = hf_hub_download(model_id, onnx_file, local_files_only=True)

        # Load id2label mapping from config.json (plain JSON, no transformers needed)
        config_path = hf_hub_download(model_id, "config.json", local_files_only=True)
        with open(config_path, encoding="utf-8") as f:
            config_data = json.load(f)
        id2label: dict[int, str] = {
            int(k): v for k, v in config_data["id2label"].items()
        }

        # Load tokenizer from tokenizer.json using the Rust tokenizers library.
        # Truncation is NOT enabled here — _OnnxClassifier processes the text
        # in _CHUNK_SIZE-token chunks, so no pre-truncation is needed and all
        # PII across arbitrarily long texts is found.
        tok_path = hf_hub_download(model_id, "tokenizer.json", local_files_only=True)
        tok = _HFTokenizer.from_file(tok_path)

        sess = ort.InferenceSession(local_path, providers=providers)
        return _OnnxClassifier(sess, tok, id2label), backend

    async def execute(self, ctx: PluginContext) -> PluginResponse:
        if self._classifier is None:
            return PluginResponse.passthrough()

        rotator = ctx.metadata.get("rotator")
        if rotator is None:
            return PluginResponse.passthrough()

        body = ctx.body
        messages = body.get("messages")
        if not messages:
            return PluginResponse.passthrough()

        vault = rotator.security.pii_vault
        reverse_index: dict = {}  # (group, value_lower) → placeholder
        counters: dict = {}       # group → current max N

        any_masked = False
        for msg in messages:
            content = msg.get("content", "")
            if not content or not isinstance(content, str):
                continue
            try:
                raw_entities = self._classifier(content)
            except Exception as exc:
                self.logger.warning(f"Inference failed on message: {exc}")
                continue

            if self._debug_input_only and raw_entities:
                role = msg.get("role", "?")
                preview = content[:120].replace("\n", " ")
                entities_summary = ", ".join(
                    f"{e['entity_group']}={repr(e['word'])}@{e['start']}-{e['end']} "
                    f"({e['score']:.2f})"
                    for e in sorted(raw_entities, key=lambda x: x["start"])
                )
                self.logger.info(
                    "[DEBUG] msg[%s] detected: %s | text: %s…",
                    role, entities_summary, preview,
                )

            if not raw_entities:
                continue
            masked = _mask_text(
                content, raw_entities, vault, reverse_index, counters,
                debug=self._debug_input_only,
            )
            if masked != content:
                msg["content"] = masked
                any_masked = True
                if self._debug_input_only:
                    role = msg.get("role", "?")
                    self.logger.info(
                        "[DEBUG] msg[%s] after masking: %s…",
                        role, masked[:120].replace("\n", " "),
                    )

        if any_masked:
            ctx.metadata["pii_masked"] = True
            categories = ", ".join(sorted(counters.keys()))
            debug_suffix = " [DEBUG: output NOT de-masked]" if self._debug_input_only else ""
            self.logger.info(f"PII masked: [{categories}] — {len(counters)} category(ies){debug_suffix}")
            await rotator._add_log(
                f"ONNX PII Masker: masked [{categories}]{debug_suffix}", level="SYSTEM"
            )
            return PluginResponse.modify(body=body)

        self.logger.debug("No PII detected in messages")
        return PluginResponse.passthrough()
