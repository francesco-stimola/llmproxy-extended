"""
Headroom Compressor — llmproxy-extended
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
"""
from typing import Any

from core.plugin_sdk import BasePlugin, PluginHook, PluginResponse
from core.plugin_engine import PluginContext


class HeadroomCompressor(BasePlugin):
    name = "headroom_compressor"
    hook = PluginHook.PRE_FLIGHT
    version = "1.2.0"
    author = "llmproxy-extended"
    description = (
        "Context compression via headroom-ai. "
        "Compresses the entire messages[] list after ONNX PII masking. "
        "use_kompress=false: structural only (SmartCrusher, LogCompressor) — no torch/ML. "
        "use_kompress=true: full ML pipeline including Kompress (ModernBERT ONNX, ~2 GB RAM). "
        "Install: pip install headroom-ai[ml,code]"
    )
    # First-pass Kompress on CPU can take 15–17 s on 10k-token contexts.
    # Structural-only (no Kompress) is instant. Keep generous headroom for both.
    timeout_ms = 30000

    def __init__(self, config: Any = None):
        super().__init__(config)
        self._compress = None
        self._import_attempted = False

    async def on_load(self) -> None:
        # Intentionally skip eager import of headroom — the Kompress ML model
        # (torch + ModernBERT ONNX, ~2 GB RAM) loads when the library is first
        # imported. Deferring to execute() means the model only loads on the
        # first request that actually meets the compression threshold.
        use_kompress = bool(self.config.get("use_kompress", True))
        self.logger.info(
            "Headroom compressor registered — mode: %s (model loads on first qualifying request)",
            "Kompress ML" if use_kompress else "structural only (no torch/ML)",
        )

    def _try_import(self) -> None:
        """Import headroom on the first execute() call that actually needs compression."""
        if self._import_attempted:
            return
        self._import_attempted = True

        use_kompress = bool(self.config.get("use_kompress", True))

        try:
            if not use_kompress:
                # Pre-configure the compress() singleton pipeline BEFORE the
                # first compress() call can create the default (Kompress-enabled)
                # one. This replaces the ContentRouter in the default transform
                # list with one that has enable_kompress=False, keeping all other
                # structural compressors (SmartCrusher, LogCompressor, etc.) active.
                self._disable_kompress_in_pipeline()

            from headroom import compress  # type: ignore[import]
            self._compress = compress
            self.logger.info(
                "Headroom: compression ready — %s",
                "full ML pipeline (Kompress enabled, torch loaded)"
                if use_kompress
                else "structural only — Kompress/torch NOT loaded (SmartCrusher, LogCompressor, SearchCompressor active)",
            )
        except ImportError:
            self.logger.warning(
                "headroom-ai not installed — plugin will passthrough. "
                "Install with: pip install headroom-ai[ml,code]"
            )

    def _disable_kompress_in_pipeline(self) -> None:
        """Pre-configure the headroom compress() singleton with Kompress disabled.

        Replaces the ContentRouter in the default TransformPipeline with one
        configured as ContentRouterConfig(enable_kompress=False). All other
        structural compressors remain active:
          - SmartCrusher   (JSON tool outputs, repeated array items)
          - LogCompressor  (build/test/error logs)
          - SearchCompressor (web search result blocks)
          - HTMLExtractor  (HTML content)

        This is thread-safe: we hold _pipeline_lock so the first compress()
        call cannot race to create the default pipeline simultaneously.
        """
        try:
            import headroom.compress as _hc  # type: ignore[import]
            from headroom.transforms.pipeline import TransformPipeline  # type: ignore[import]
            from headroom.transforms.content_router import ContentRouter, ContentRouterConfig  # type: ignore[import]
            from headroom.config import HeadroomConfig  # type: ignore[import]

            with _hc._pipeline_lock:
                if _hc._pipeline is not None:
                    # Pipeline already initialized (another call beat us).
                    # We cannot safely replace it now — log and proceed.
                    self.logger.warning(
                        "Headroom pipeline already initialized before no-Kompress setup; "
                        "Kompress may be active"
                    )
                    return

                # Build the default pipeline (includes CacheAligner with correct
                # defaults from HeadroomConfig), then swap ContentRouter.
                pipeline = TransformPipeline(config=HeadroomConfig())
                for i, transform in enumerate(pipeline.transforms):
                    if type(transform).__name__ == "ContentRouter":
                        pipeline.transforms[i] = ContentRouter(
                            ContentRouterConfig(enable_kompress=False)
                        )
                        self.logger.info(
                            "Headroom pipeline configured: Kompress (ML) disabled — "
                            "SmartCrusher, LogCompressor, SearchCompressor remain active"
                        )
                        break

                _hc._pipeline = pipeline

        except Exception as exc:
            self.logger.warning(
                "Could not configure no-Kompress pipeline (%s); "
                "Kompress may load on the first compression request",
                exc,
            )

    async def execute(self, ctx: PluginContext) -> PluginResponse:
        body = ctx.body
        messages = body.get("messages")
        if not messages:
            return PluginResponse.passthrough()

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
        try:
            result = self._compress(messages, model=model)
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
