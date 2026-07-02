"""
Headroom Compressor — llmproxy-extended  (v1.4.0)
PRE_FLIGHT plugin: compresses the full message list using headroom-ai after PII masking.

Run order in manifest.yaml:
  priority 19 — OnnxPiiMasker      (mask PII first)
  priority 25 — HeadroomCompressor (compress already-anonymized messages)

This ensures CCR cache and headroom's local model never see original PII —
only already-masked versions are compressed and cached.

Compression benchmarks (headroom-ai docs):
  Code search:         92% reduction (17 765 → 1 408 tokens)
  SRE incident debug:  92% reduction (65 694 → 5 118 tokens)
  GitHub issue triage: 73% reduction
  Average:             87% reduction

Requirements: pip install "headroom-ai[ml,code]"
API: compress(messages, model=...) → CompressResult

Config options (manifest.yaml):
  min_tokens_to_compress: int  — skip requests shorter than this (default: 200 words)
  use_kompress: bool           — enable Kompress ML model (default: true)
                                 Set false to use only structural compressors
                                 (SmartCrusher, LogCompressor, etc.) without
                                 loading torch/ModernBERT (~2 GB RAM saved).
                                 Implemented via the official CompressConfig
                                 kompress_model='disabled' kwarg — no singleton
                                 hacking required.

JSON object pre-processing (v1.4.0):
  headroom's ContentRouter routes to SmartCrusher only when the root of a
  content string is a JSON array (`[{...}]`). MCP tool results arrive as
  `{"ok":true,"data":{...}}` — a JSON object root — so ContentRouter picks
  the PLAIN_TEXT fallback and SmartCrusher never fires.

  To fill the gap, execute() now calls SmartCrusher.compact_document_json()
  directly on every tool_result block whose content is a JSON object before
  passing the (already compacted) messages to headroom.compress().
  compact_document_json() is headroom's lossless recursive walker: it finds
  tabular sub-arrays anywhere in the document tree and rewrites them as
  CSV+schema strings without dropping rows or emitting CCR markers
  (lossless_only=True). JSON arrays that are already handled by ContentRouter
  are skipped here to avoid double-processing.
"""
import asyncio
import json
from typing import Any

from core.plugin_sdk import BasePlugin, PluginHook, PluginResponse
from core.plugin_engine import PluginContext


class HeadroomCompressor(BasePlugin):
    name = "headroom_compressor"
    hook = PluginHook.PRE_FLIGHT
    version = "1.4.0"
    author = "llmproxy-extended"
    description = (
        "Context compression via headroom-ai. "
        "Compresses the entire messages[] list after ONNX PII masking. "
        "use_kompress=false: structural only (SmartCrusher, LogCompressor) — no torch/ML download. "
        "use_kompress=true: full ML pipeline including Kompress (ModernBERT ONNX). "
        "Install: pip install headroom-ai[ml,code]"
    )
    # First-pass Kompress on CPU can take 15-17s on 10k-token contexts on
    # server-class hardware, longer on a laptop under load. Compress calls run
    # via run_in_executor (see execute()), so a generous timeout no longer
    # risks freezing the event loop — it only means a slower response. Set
    # above HEADROOM_COMPRESSION_DEADLINE_MS (Kompress's own internal
    # wall-clock budget, default 20000ms, see .env.example) so this plugin
    # timeout never cuts Kompress off before its own deadline does.
    timeout_ms = 130000

    def __init__(self, config: Any = None):
        super().__init__(config)
        self._compress = None
        self._import_attempted = False
        self._crusher = None
        self._crusher_load_attempted = False

    async def on_load(self) -> None:
        use_kompress = bool(self.config.get("use_kompress", True))
        self._try_import()  # eager import at startup — no per-request latency spike
        if self._compress is None:
            return

        if use_kompress:
            # Trigger Kompress background model load now so the first real request
            # finds the ModernBERT ONNX model already warm. headroom starts its own
            # background thread internally; the executor call here just delivers the
            # trigger without blocking the event loop.
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._compress([{"role": "user", "content": "warmup"}], model="gpt-4o"),
            )
            self.logger.info("Headroom: Kompress model loading in background (warmup triggered)")
        else:
            self.logger.info("Headroom: structural compressor ready — Kompress disabled")

    def _try_import(self) -> None:
        """Import headroom on the first execute() call that actually needs compression."""
        if self._import_attempted:
            return
        self._import_attempted = True

        use_kompress = bool(self.config.get("use_kompress", True))

        try:
            from headroom import compress  # type: ignore[import]

            self._compress = compress
            self.logger.info(
                "Headroom: compression ready — %s",
                "full ML pipeline (Kompress enabled)"
                if use_kompress
                else "structural only — Kompress disabled via kompress_model='disabled' (SmartCrusher, LogCompressor, SearchCompressor active)",
            )
        except ImportError:
            self.logger.warning(
                "headroom-ai not installed — plugin will passthrough. "
                "Install with: pip install headroom-ai[ml,code]"
            )

    def _get_crusher(self):
        """Lazy-load a lossless SmartCrusher instance (one per plugin lifetime)."""
        if self._crusher_load_attempted:
            return self._crusher
        self._crusher_load_attempted = True
        try:
            from headroom.transforms.smart_crusher import SmartCrusher, SmartCrusherConfig
            self._crusher = SmartCrusher(config=SmartCrusherConfig(lossless_only=True))
            self.logger.info("Headroom: SmartCrusher (lossless) ready for JSON-object pre-processing")
        except Exception as exc:
            self.logger.warning("SmartCrusher not available, JSON-object pre-processing disabled: %s", exc)
            self._crusher = None
        return self._crusher

    def _try_compact_json_object(self, crusher, text: str, min_bytes: int) -> str:
        """Compact `text` with compact_document_json() if it is a JSON object.

        Skips JSON arrays (ContentRouter already routes those to SmartCrusher).
        Returns the original string on any failure so the plugin stays fault-tolerant.
        """
        stripped = text.strip()
        if len(stripped) < min_bytes or not stripped.startswith("{"):
            return text
        try:
            json.loads(stripped)
        except (ValueError, TypeError):
            return text
        try:
            compacted: str = str(crusher.compact_document_json(stripped))
            saved = len(stripped) - len(compacted)
            if saved > 0:
                self.logger.debug(
                    "SmartCrusher compact_document_json: %d → %d bytes (saved %d)",
                    len(stripped), len(compacted), saved,
                )
            return compacted
        except Exception as exc:
            self.logger.debug("compact_document_json failed (non-fatal): %s", exc)
            return text

    def _preprocess_tool_results(self, messages: list, min_bytes: int = 500) -> None:
        """Walk messages and compact JSON-object tool_result content via SmartCrusher.

        ContentRouter only routes root-level JSON arrays to SmartCrusher; MCP
        results wrapped in {"ok":true,"data":{...}} are classified as PLAIN_TEXT
        and never reach SmartCrusher. This method fills the gap by calling
        compact_document_json() directly before headroom.compress() runs.
        """
        crusher = self._get_crusher()
        if crusher is None:
            return
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                nested = block.get("content")
                if isinstance(nested, str):
                    block["content"] = self._try_compact_json_object(crusher, nested, min_bytes)
                elif isinstance(nested, list):
                    for nb in nested:
                        if isinstance(nb, dict) and nb.get("type") == "text":
                            nb_text = nb.get("text", "")
                            if nb_text:
                                nb["text"] = self._try_compact_json_object(crusher, nb_text, min_bytes)

    async def execute(self, ctx: PluginContext) -> PluginResponse:
        body = ctx.body
        messages = body.get("messages")
        if not messages:
            return PluginResponse.passthrough()

        loop = asyncio.get_event_loop()

        # Pre-process tool_result blocks that contain JSON objects so that
        # SmartCrusher's recursive walker compacts nested tabular arrays before
        # ContentRouter runs. This runs unconditionally (independent of the
        # word-count threshold below) because a large MCP result can sit inside
        # a short conversation that would otherwise skip Kompress entirely.
        # Runs off the event loop thread — see the run_in_executor note below.
        await loop.run_in_executor(None, self._preprocess_tool_results, messages)

        # Short-circuit before loading any model if context is below threshold.
        # This is the RAM-saving fast path: short requests never trigger import.
        min_words: int = self.config.get("min_tokens_to_compress", 200)
        total_words = sum(
            len((m.get("content") or "").split())
            for m in messages
            if isinstance(m.get("content"), str)
        )
        if total_words < min_words:
            self.logger.debug(
                "Skipping compression: %d words < %d threshold", total_words, min_words
            )
            return PluginResponse.passthrough()

        # Lazy-load on the first request that actually needs compression
        if self._compress is None:
            self._try_import()
        if self._compress is None:
            return PluginResponse.passthrough()

        model: str = body.get("model", "gpt-4o")
        use_kompress = bool(self.config.get("use_kompress", True))

        try:
            if use_kompress:
                # Full pipeline: Kompress ML + structural compressors.
                # Kompress ONNX inference is synchronous CPU-bound work that can
                # take 15-20s+ on large contexts. Running it inline would block
                # the whole asyncio event loop — freezing every other concurrent
                # request on the proxy (including unrelated upstream connections)
                # for the entire duration, and asyncio.wait_for's plugin timeout
                # can't preempt a blocking call that never yields control back.
                # run_in_executor moves it to a worker thread so the event loop
                # stays responsive and the engine's timeout_ms is enforceable.
                result = await loop.run_in_executor(
                    None, lambda: self._compress(messages, model=model)
                )
            else:
                # Structural only: pass kompress_model='disabled' per-call.
                # This is the official CompressConfig API — headroom's ContentRouter
                # checks _runtime_kompress_model == 'disabled' and returns None from
                # _get_kompress(), preventing both Kompress inference AND the
                # background model download (ensure_background_load is never called).
                result = await loop.run_in_executor(
                    None,
                    lambda: self._compress(messages, model=model, kompress_model="disabled"),
                )
        except Exception as exc:
            self.logger.warning("Headroom compression failed: %s", exc)
            return PluginResponse.passthrough()

        compressed_messages = result.messages
        tokens_saved = getattr(result, "tokens_saved", 0)
        ratio = getattr(result, "compression_ratio", 1.0)

        if tokens_saved > 0:
            self.logger.info(
                "Headroom: %d tokens saved (%d → %d, ratio=%.2f)",
                tokens_saved,
                getattr(result, "tokens_before", 0),
                getattr(result, "tokens_after", 0),
                ratio,
            )
            ctx.metadata["headroom_compressed"] = True
            ctx.metadata["headroom_tokens_saved"] = tokens_saved
            body["messages"] = compressed_messages
            return PluginResponse.modify(body=body)

        self.logger.debug("Headroom: no compression applied (context too small or already optimal)")
        return PluginResponse.passthrough()
