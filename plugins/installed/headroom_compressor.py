"""
Headroom Compressor — llmproxy-extended (disabled by default)
PRE_FLIGHT plugin: compresses LLM context using Headroom after PII masking.

Run order in manifest.yaml:
  priority 19 — OnnxPiiMasker    (mask PII first)
  priority 25 — HeadroomCompressor (compress already-anonymized text)

This ensures the Headroom CCR local cache never stores original PII —
only the already-masked versions are compressed and cached.

Compression benchmarks (from Headroom docs):
  Code search:        92% reduction (17 765 → 1 408 tokens)
  SRE incident debug: 92% reduction (65 694 → 5 118 tokens)
  GitHub issue triage: 73% reduction
  Average:            87% reduction

Requirements: pip install "headroom[ml]"
Model:        Kompress-v2-base (ModernBERT, downloads automatically)
"""
from __future__ import annotations

from core.plugin_sdk import BasePlugin, PluginHook, PluginResponse
from core.plugin_engine import PluginContext


class HeadroomCompressor(BasePlugin):
    name = "headroom_compressor"
    hook = PluginHook.PRE_FLIGHT
    version = "1.0.0"
    author = "llmproxy-extended"
    description = (
        "Context compression via Headroom (60–95% token reduction). "
        "Always runs after ONNX PII masking (priority 25 > 19). "
        "Install: pip install headroom[ml]"
    )
    timeout_ms = 10000  # ML compression on large contexts can take seconds

    def __init__(self, config=None):
        super().__init__(config)
        self._compress = None

    async def on_load(self) -> None:
        try:
            # headroom exposes a synchronous compress() for single-string use.
            # For async / streaming contexts headroom.acompress() may be preferred
            # once the library stabilises that API.
            from headroom import compress  # type: ignore[import]
            self._compress = compress
            self.logger.info("Headroom compressor ready")
        except ImportError:
            self.logger.warning(
                "headroom not installed — plugin will passthrough. "
                "Install with: pip install headroom[ml]"
            )

    async def execute(self, ctx: PluginContext) -> PluginResponse:
        if self._compress is None:
            return PluginResponse.passthrough()

        body = ctx.body
        messages = body.get("messages")
        if not messages:
            return PluginResponse.passthrough()

        min_tokens: int = self.config.get("min_tokens_to_compress", 200)
        any_compressed = False
        new_messages = []

        for msg in messages:
            content = msg.get("content", "")
            if not content or not isinstance(content, str):
                new_messages.append(msg)
                continue

            # Skip very short messages — compression overhead > benefit.
            if len(content.split()) < min_tokens:
                new_messages.append(msg)
                continue

            try:
                compressed = self._compress(content)
                if compressed and compressed != content:
                    any_compressed = True
                    new_messages.append({**msg, "content": compressed})
                else:
                    new_messages.append(msg)
            except Exception as exc:
                self.logger.warning(f"Headroom failed on message: {exc}")
                new_messages.append(msg)

        if any_compressed:
            body["messages"] = new_messages
            ctx.metadata["headroom_compressed"] = True
            return PluginResponse.modify(body=body)

        return PluginResponse.passthrough()
