"""Anthropic-native /v1/messages route.

Accepts requests in Anthropic Messages API format (as sent by Claude Code,
Anthropic SDK clients, etc.) and proxies them through the plugin pipeline
(PII masking, budget guard, agentic loop breaker, headroom compression, …)
before forwarding to the configured Anthropic endpoint.

This is the companion to /v1/chat/completions: both run the same plugin
rings, but this route speaks Anthropic wire format end-to-end so the client
never needs to convert.

Key design decisions:
  - INGRESS ring runs first (handles ingress-auth plugin when enabled)
  - PRE_FLIGHT ring runs on the body (PII masker iterates messages[]; we
    also inject the top-level `system` field as a temporary message so it
    gets masked too, then restore it afterwards)
  - Forward directly to Anthropic in Anthropic format (no adapter round-trip)
  - POST_FLIGHT ring runs on non-streaming responses (demasking)
  - Streaming: chunks are forwarded as-is; demasking on stream is a TODO
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import time
import uuid
import os
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.metrics import MetricsTracker
from core.plugin_engine import PluginContext, PluginHook

logger = logging.getLogger("llmproxy.routes.messages")


def create_router(agent) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/messages")
    async def anthropic_messages(request: Request):
        """Anthropic-native proxy with plugin pipeline."""
        start_time = time.time()

        # --- Key resolution ---
        # Clients may use x-api-key (Anthropic SDK) or Authorization: Bearer
        client_key = request.headers.get("x-api-key", "")
        auth_header = request.headers.get("authorization", "")
        if not client_key:
            client_key = auth_header.removeprefix("Bearer ").removeprefix("bearer ").strip()

        # --- Session fingerprint ---
        if client_key:
            session_id = hashlib.sha256(client_key.encode()).hexdigest()[:16]
        else:
            ip = request.client.host if request.client else "anon"
            ua = request.headers.get("user-agent", "")
            session_id = hashlib.sha256(f"{ip}:{ua}".encode()).hexdigest()[:16]

        # --- Parse body ---
        try:
            body: Dict[str, Any] = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        # --- Inject system field into messages[] for PII plugin compatibility ---
        # PRE_FLIGHT plugins iterate body["messages"]; the Anthropic-native
        # `system` field sits outside that array.  We temporarily move it in,
        # mask it alongside the other messages, then restore the top-level
        # field after PRE_FLIGHT finishes.
        system_injected = False
        original_system = body.get("system")
        if original_system:
            system_text = (
                original_system
                if isinstance(original_system, str)
                else " ".join(
                    b.get("text", "") for b in original_system if isinstance(b, dict)
                )
            )
            body = dict(body)
            body["messages"] = [
                {"role": "system", "content": system_text},
                *body.get("messages", []),
            ]
            system_injected = True

        # --- Plugin context ---
        ctx = PluginContext(
            request=request,
            body=body,
            session_id=session_id,
            metadata={
                "rotator": agent,
                "req_id": uuid.uuid4().hex[:16],
                "_cache_control": request.headers.get("cache-control", ""),
                "_anthropic_native": True,
            },
            state=agent.plugin_state,
        )

        # --- RING 1: INGRESS ---
        await agent.plugin_manager.execute_ring(PluginHook.INGRESS, ctx)
        if ctx.stop_chain:
            raise HTTPException(status_code=403, detail=ctx.error or "Ingress Blocked")

        # --- RING 2: PRE_FLIGHT (PII masking, budget guard, …) ---
        await agent.plugin_manager.execute_ring(PluginHook.PRE_FLIGHT, ctx)
        if ctx.stop_chain:
            raise HTTPException(
                status_code=ctx.metadata.get("_block_status", 403),
                detail=ctx.error or "Request blocked by pre-flight plugin",
            )

        # --- Restore system field after masking ---
        processed_body = dict(ctx.body)
        if system_injected:
            msgs = list(processed_body.get("messages", []))
            # The first message was our injected system entry
            system_msg = msgs[0] if msgs else None
            processed_body["messages"] = msgs[1:] if len(msgs) > 1 else []
            if system_msg:
                # Restore as top-level system with the (now masked) content
                if isinstance(original_system, str):
                    processed_body["system"] = system_msg.get("content", original_system)
                else:
                    processed_body["system"] = [
                        {"type": "text", "text": system_msg.get("content", "")}
                    ]

        # --- Resolve Anthropic endpoint ---
        anthropic_cfg = agent.config.get("endpoints", {}).get("anthropic", {})
        base_url = anthropic_cfg.get("base_url", "https://api.anthropic.com/v1")
        upstream_url = f"{base_url.rstrip('/')}/messages"

        api_key_env = anthropic_cfg.get("api_key_env", "ANTHROPIC_API_KEY")
        proxy_key = os.environ.get(api_key_env, "") if api_key_env else ""

        # --- Forward headers ---
        # Auth strategy:
        #   1. Proxy holds its own API key → always use x-api-key (standard key)
        #   2. Client sent Authorization: Bearer → preserve it (OAuth token, oat01)
        #   3. Client sent x-api-key → forward as x-api-key (classic API key)
        # Mixing oauth tokens into x-api-key causes Anthropic 401 "invalid x-api-key".
        forward_headers: Dict[str, str] = {
            "anthropic-version": request.headers.get("anthropic-version", "2023-06-01"),
            "content-type": "application/json",
        }
        if proxy_key:
            forward_headers["x-api-key"] = proxy_key
            effective_key = proxy_key
        elif auth_header and auth_header.lower().startswith("bearer "):
            forward_headers["authorization"] = auth_header
            effective_key = client_key  # same value, used only for logging
        elif client_key:
            forward_headers["x-api-key"] = client_key
            effective_key = client_key
        else:
            raise HTTPException(
                status_code=401,
                detail="No API key: set ANTHROPIC_API_KEY on the proxy or pass x-api-key / Authorization: Bearer",
            )

        for hdr in ("anthropic-beta", "anthropic-dangerous-direct-browser-access"):
            if hdr in request.headers:
                forward_headers[hdr] = request.headers[hdr]

        is_streaming = processed_body.get("stream", False)
        session = await agent._get_session()
        req_id = ctx.metadata.get("req_id", "")

        # --- Streaming passthrough ---
        if is_streaming:
            async def _stream():
                import aiohttp as _aiohttp  # noqa: PLC0415 — local to avoid circular at module level
                try:
                    async with session.post(
                        upstream_url, json=processed_body, headers=forward_headers
                    ) as resp:
                        if resp.status >= 400:
                            err = await resp.text()
                            logger.warning(
                                "Anthropic upstream error %s: %s", resp.status, err[:200]
                            )
                            yield (
                                f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': err}})}\n\n"
                            ).encode()
                            return
                        async for chunk in resp.content:
                            yield chunk
                except asyncio.CancelledError:
                    # Client disconnected — abort silently
                    return
                except (TimeoutError, _aiohttp.ClientError) as exc:
                    # aiohttp wraps CancelledError → TimeoutError when its timer is
                    # active; catch both that and aiohttp-level errors gracefully.
                    logger.warning("Upstream connection error (stream): %s", exc)
                    try:
                        yield (
                            f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'connection_error', 'message': str(exc)}})}\n\n"
                        ).encode()
                    except Exception:
                        pass

            MetricsTracker.track_request(
                "POST", "/v1/messages", 200, time.time() - start_time
            )
            return StreamingResponse(
                _stream(),
                media_type="text/event-stream",
                headers={
                    "X-LLMProxy-Request-Id": req_id,
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        # --- Non-streaming ---
        async with session.post(
            upstream_url, json=processed_body, headers=forward_headers
        ) as resp:
            raw = await resp.read()
            status = resp.status
            content_type = resp.headers.get("content-type", "application/json")

        if status >= 400:
            logger.warning(
                "Anthropic upstream error %s: %s", status,
                raw.decode("utf-8", errors="replace")[:200],
            )

        try:
            response_data = json.loads(raw)
        except json.JSONDecodeError:
            response_data = {"error": raw.decode("utf-8", errors="replace")}

        response = JSONResponse(
            content=response_data,
            status_code=status,
            headers={"X-LLMProxy-Request-Id": req_id},
        )

        # --- RING 4: POST_FLIGHT (PII demasking, sanitization) ---
        ctx.response = response
        ctx.body = processed_body
        await agent.plugin_manager.execute_ring(PluginHook.POST_FLIGHT, ctx)

        MetricsTracker.track_request(
            "POST", "/v1/messages", status, time.time() - start_time
        )
        return ctx.response or response

    return router
