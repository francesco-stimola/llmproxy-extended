"""
Headroom Compressor — llmproxy-extended (disabled by default)
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
"""
from typing import Any

from core.plugin_sdk import BasePlugin, PluginHook, PluginResponse
from core.plugin_engine import PluginContext


class HeadroomCompressor(BasePlugin):
    name = "headroom_compressor"
    hook = PluginHook.PRE_FLIGHT
    version = "1.1.0"
    author = "llmproxy-extended"
    description = (
        "Context compression via headroom-ai (60–95% token reduction). "
        "Compresses the entire messages[] list after ONNX PII masking. "
        "Install: pip install headroom-ai[ml,code]"
    )
    # 30 s: first-pass Kompress on CPU can take 15–17 s on 10k-token contexts.
    timeout_ms = 30000

    def __init__(self, config: Any = None):
        super().__init__(config)
        self._compress = None
        # _import_attempted prevents re-importing after a failed ImportError
        self._import_attempted = False

    async def on_load(self) -> None:
        # Intentionally skip eager import of headroom — the Kompress ML model
        # (~1-2 GB RAM, possibly including PyTorch) loads when the library is
        # first imported.  Deferring to execute() means the model only loads
        # when a request actually meets the compression threshold, saving RAM
        # on idle or low-volume instances.
        self.logger.info(
            "Headroom compressor registered (model loads on first qualifying request)"
        )

    def _try_import(self) -> None:
        """Import headroom on the first execute() call that needs it."""
        if self._import_attempted:
            return
        self._import_attempted = True
        try:
            from headroom import compress  # type: ignore[import]
            self._compress = compress
            self.logger.info("Headroom: ML model loaded (first compression request)")
        except ImportError:
            self.logger.warning(
                "headroom-ai not installed — plugin will passthrough. "
                "Install with: pip install headroom-ai[ml,code]"
            )

    async def execute(self, ctx: PluginContext) -> PluginResponse:
        body = ctx.body
        messages = body.get("messages")
        if not messages:
            return PluginResponse.passthrough()

        # Skip early (before loading the model) if the context is too short.
        # This is the main RAM-saving path: short requests never trigger the import.
        min_words: int = self.config.get("min_tokens_to_compress", 200)
        total_words = sum(
            len((m.get("content") or "").split())
            for m in messages
            if isinstance(m.get("content"), str)
        )
        if total_words < min_words:
            self.logger.debug(
                f"Skipping compression: {total_words} words < {min_words} threshold"
            )
            return PluginResponse.passthrough()

        # Lazy-load Kompress on the first request that actually needs compression
        if self._compress is None:
            self._try_import()
        if self._compress is None:
            return PluginResponse.passthrough()

        model: str = body.get("model", "gpt-4o")
        try:
            result = self._compress(messages, model=model)
        except Exception as exc:
            self.logger.warning(f"Headroom compression failed: {exc}")
            return PluginResponse.passthrough()

        compressed_messages = result.messages
        tokens_saved = getattr(result, "tokens_saved", 0)
        ratio = getattr(result, "compression_ratio", 1.0)

        if tokens_saved > 0:
            self.logger.info(
                f"Headroom: {tokens_saved} tokens saved "
                f"({getattr(result, 'tokens_before', 0)} → {getattr(result, 'tokens_after', 0)}, "
                f"ratio={ratio:.2f})"
            )
            ctx.metadata["headroom_compressed"] = True
            ctx.metadata["headroom_tokens_saved"] = tokens_saved
            body["messages"] = compressed_messages
            return PluginResponse.modify(body=body)

        self.logger.debug(f"Headroom: no compression applied (context too small or already optimal)")
        return PluginResponse.passthrough()
