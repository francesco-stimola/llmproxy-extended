"""
Model pricing table — per-million-token costs for budget tracking.

Prices are in USD per million tokens ($/MTok). Updated July 2026.
Override via config.yaml `pricing` section for custom/private deployments.
"""

import logging
from typing import Dict, Set

logger = logging.getLogger("llmproxy.pricing")

# Models that have already triggered the unknown-pricing warning. Keeps the
# log signal-to-noise ratio sane: we want to tell the operator "your model
# 'foo' is using default pricing" exactly once per process, not on every
# request that names it. Reset on process restart.
_DEFAULT_PRICING_WARNED: Set[str] = set()

# Pricing data is loaded at startup from data/pricing.yaml via SignatureStore
# (set_model_pricing). Edit that file to add or update model prices — changes
# are hot-reloaded every 30 s without a proxy restart.
MODEL_PRICING: Dict[str, Dict[str, float]] = {}

# Default fallback for unknown models
_DEFAULT_PRICING = {"input": 1.00, "output": 3.00}

# Runtime config overrides (populated by RotatorAgent.setup() from config.yaml)
_config_overrides: Dict[str, Dict[str, float]] = {}

# Pre-sorted prefix list for O(log n) longest-prefix matching via bisect.
# Sorted longest-first so the first match is the most specific.
_SORTED_PREFIXES: list = sorted(MODEL_PRICING.keys(), key=len, reverse=True)


def set_config_overrides(overrides: Dict[str, Dict[str, float]]):
    """Apply pricing overrides from config.yaml at startup."""
    _config_overrides.update(overrides)
    global _SORTED_PREFIXES
    all_prefixes = set(MODEL_PRICING.keys()) | set(_config_overrides.keys())
    _SORTED_PREFIXES = sorted(all_prefixes, key=len, reverse=True)


def get_pricing(model: str) -> Dict[str, float]:
    """Get pricing for a model. Priority: config override > exact match > longest prefix > default.

    Prefix matching is O(P) where P = number of known models (~40), but the
    list is sorted longest-first so the first hit is the most specific match
    (e.g. "gpt-4o-mini" matches before "gpt-4o" for "gpt-4o-mini-2025-01").
    Exact lookups are O(1) dict hits and cover the common case.

    On default-pricing fallback (unknown model), emits a one-shot WARNING
    per model name so operators see "your routing scores + budget for
    'foo' are guesses" instead of silently absorbing $1/$3 estimates.
    """
    if model in _config_overrides:
        return _config_overrides[model]
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    # Longest-prefix matching for versioned model names (e.g. "gpt-4o-2024-08-06")
    for prefix in _SORTED_PREFIXES:
        if model.startswith(prefix):
            if prefix in _config_overrides:
                return _config_overrides[prefix]
            return MODEL_PRICING[prefix]
    # Unknown model — fall back to default and warn (once).
    if model and model not in _DEFAULT_PRICING_WARNED:
        _DEFAULT_PRICING_WARNED.add(model)
        logger.warning(
            "Unknown model '%s' — using default pricing $%.2f/$%.2f per MTok. "
            "Routing scores and budget estimates for this model are guesses. "
            "Add it to MODEL_PRICING or config.yaml `pricing` to fix.",
            model,
            _DEFAULT_PRICING["input"],
            _DEFAULT_PRICING["output"],
        )
    return _DEFAULT_PRICING


def baseline_premium_pricing() -> Dict[str, float]:
    """The most-expensive paid model in MODEL_PRICING — used as the
    'what if I'd run everything on the premium tier' baseline for the
    /cost-efficiency savings estimate. Free models ($0) are excluded —
    they'd give a misleadingly cheap baseline.
    """
    paid = [p for p in MODEL_PRICING.values() if p.get("input", 0) > 0]
    if not paid:
        return _DEFAULT_PRICING
    max_input = max(p["input"] for p in paid)
    max_output = max(p["output"] for p in paid)
    return {"input": max_input, "output": max_output}


def estimate_baseline_savings(
    spend_rows: list,
) -> Dict[str, float]:
    """Compute model-mix savings vs a premium-tier baseline.

    For each spend row, computes what the same prompt+completion tokens
    would have cost at the most-expensive paid model's rates. Sums
    across rows, returns:

        baseline_usd : hypothetical premium-tier total
        actual_usd   : observed total
        saved_usd    : baseline − actual (clamped ≥ 0; negative means the
                       user actually picked premium so there's nothing
                       to brag about)
        saved_pct    : saved / baseline × 100, 0 when baseline is 0

    Honest scope: this measures **model-mix economics**, not just the
    slider's effect. The slider only influences ties between endpoints
    serving the same model; user choice drives most of the savings.
    Frame the number as "your multi-provider strategy is saving you X%
    vs going all-premium" — not "the slider saves X."

    `spend_rows` is the output of `store.query_spend(group_by='model')`.
    """
    baseline_pricing = baseline_premium_pricing()
    max_in = baseline_pricing["input"]
    max_out = baseline_pricing["output"]

    baseline_total = 0.0
    actual_total = 0.0
    for row in spend_rows:
        prompt = row.get("total_prompt_tokens", 0) or 0
        completion = row.get("total_completion_tokens", 0) or 0
        actual = row.get("total_cost_usd", 0.0) or 0.0
        baseline = (prompt / 1_000_000.0) * max_in + (
            completion / 1_000_000.0
        ) * max_out
        baseline_total += baseline
        actual_total += actual

    saved = max(0.0, baseline_total - actual_total)
    saved_pct = (saved / baseline_total * 100.0) if baseline_total > 0 else 0.0
    return {
        "baseline_usd": round(baseline_total, 4),
        "actual_usd": round(actual_total, 4),
        "saved_usd": round(saved, 4),
        "saved_pct": round(saved_pct, 2),
        "baseline_input_per_mtok": max_in,
        "baseline_output_per_mtok": max_out,
    }


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate cost in USD from token counts.

    Args:
        model: Model identifier (e.g. "gpt-4o", "claude-sonnet-4-20250514")
        prompt_tokens: Number of input/prompt tokens
        completion_tokens: Number of output/completion tokens

    Returns:
        Estimated cost in USD
    """
    pricing = get_pricing(model)
    return (prompt_tokens / 1_000_000) * pricing["input"] + (
        completion_tokens / 1_000_000
    ) * pricing["output"]


def estimate_cost_pre_flight(
    model: str, input_tokens: int, avg_output_ratio: float = 0.5
) -> float:
    """Pre-flight cost estimate (before we know actual output tokens).

    Used by SmartBudgetGuard to estimate cost before the request is sent.
    """
    pricing = get_pricing(model)
    est_output_tokens = int(input_tokens * avg_output_ratio)
    return (input_tokens / 1_000_000) * pricing["input"] + (
        est_output_tokens / 1_000_000
    ) * pricing["output"]


def set_model_pricing(pricing: dict):
    """Merge external pricing into MODEL_PRICING and re-sort _SORTED_PREFIXES.

    Entries in `pricing` override the static table; models absent from `pricing`
    keep their static defaults. This means data/pricing.yaml is additive — a
    missing entry there no longer silently drops a model from the table.
    """
    global MODEL_PRICING, _SORTED_PREFIXES
    MODEL_PRICING.update(pricing)
    # Convert all keys and values to proper formats
    for k, v in list(MODEL_PRICING.items()):
        if isinstance(v, dict):
            MODEL_PRICING[k] = {
                "input": float(v.get("input", 0.0)),
                "output": float(v.get("output", 0.0)),
            }
    _SORTED_PREFIXES[:] = sorted(MODEL_PRICING.keys(), key=len, reverse=True)
    logger.info("Hot-reloaded model pricing table: %d entries", len(MODEL_PRICING))
