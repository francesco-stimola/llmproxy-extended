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
    timeout_ms = 10000  # ML compression on large contexts can take several seconds

    def __init__(self, config: Any = None):
        super().__init__(config)
        self._compress = None

    async def on_load(self) -> None:
        try:
            from headroom import compress  # type: ignore[import]
            self._compress = compress
            self.logger.info("Headroom compressor ready (headroom-ai)")
        except ImportError:
            self.logger.warning(
                "headroom-ai not installed — plugin will passthrough. "
                "Install with: pip install headroom-ai[ml,code]"
            )

    async def execute(self, ctx: PluginContext) -> PluginResponse:
        if self._compress is None:
            return PluginResponse.passthrough()

        body = ctx.body
        messages = body.get("messages")
        if not messages:
            return PluginResponse.passthrough()

        # Skip if total word count is below threshold (compression overhead > benefit)
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
