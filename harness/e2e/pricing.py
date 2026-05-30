"""Canonical model pricing (USD per million tokens).

Single source of truth for all cost calculations across the harness.
All log parsers and collect_results import from here.

Field conventions (all values in USD per 1M tokens):
    input          -- non-cached input tokens
    output         -- output / completion tokens
    cache_read     -- cached input tokens (read from cache)
    cache_write    -- cache creation tokens (default write price); optional
    cache_write_5m -- 5-minute ephemeral cache write (Anthropic Claude only); optional
    cache_write_1h -- 1-hour ephemeral cache write (Anthropic Claude only); optional
    tiers          -- list of {threshold, input, output, cache_read} for tiered pricing;
                      threshold is max prompt_tokens for that tier

When both cache_write_5m and cache_write_1h are present, callers should use them
for accurate Claude cost calculation. Otherwise, cache_write is the fallback.
"""

from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# All model pricing
# ---------------------------------------------------------------------------

MODEL_PRICING: Dict[str, Dict[str, Any]] = {
    # ── Anthropic Claude ──────────────────────────────────────────────────
    "claude-sonnet": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.30,
        "cache_write": 3.75,       # default (used when 5m/1h breakdown unavailable)
        "cache_write_5m": 3.75,    # 1.25x input
        "cache_write_1h": 6.0,     # 2.0x input
    },
    "claude-opus": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.50,
        "cache_write": 6.25,
        "cache_write_5m": 6.25,    # 1.25x input
        "cache_write_1h": 10.0,    # 2.0x input
    },
    "claude-haiku": {
        "input": 1.0,
        "output": 5.0,
        "cache_read": 0.10,
        "cache_write": 1.25,
        "cache_write_5m": 1.25,    # 1.25x input
        "cache_write_1h": 2.0,     # 2.0x input
    },
    # ── OpenAI GPT ────────────────────────────────────────────────────────
    # Verified against https://developers.openai.com/api/docs/models 2026-04.
    # GPT-5.4: prompts above 272K billed at 2x input and 1.5x output.
    "gpt-5.4": {
        "tiers": [
            {"threshold": 272_000, "input": 2.50, "cache_read": 0.25, "output": 15.00},
            {"threshold": float("inf"), "input": 5.00, "cache_read": 0.50, "output": 22.50},
        ],
    },
    # GPT-5.5 (released 2026-04-23): flat pricing across full context.
    "gpt-5.5": {"input": 5.00, "cache_read": 0.50, "output": 30.00},
    "gpt-5.3-codex": {"input": 1.75, "cache_read": 0.175, "output": 14.00},
    "gpt-5.2-codex": {"input": 1.75, "cache_read": 0.175, "output": 14.00},
    "gpt-5.2": {"input": 1.75, "cache_read": 0.175, "output": 14.00},
    "gpt-5.2-pro": {"input": 21.00, "cache_read": 2.10, "output": 168.00},
    "gpt-4o": {"input": 2.50, "cache_read": 1.25, "output": 10.00},
    "gpt-4o-mini": {"input": 0.15, "cache_read": 0.075, "output": 0.60},
    "gpt-4-turbo": {"input": 10.00, "cache_read": 5.00, "output": 30.00},
    "gpt-4": {"input": 30.00, "cache_read": 15.00, "output": 60.00},
    "gpt-3.5-turbo": {"input": 0.50, "cache_read": 0.25, "output": 1.50},
    # ── Google Gemini ─────────────────────────────────────────────────────
    # https://ai.google.dev/gemini-api/docs/pricing
    # gemini-3.5-flash — official Standard-tier pricing
    # (ai.google.dev/gemini-api/docs/pricing, retrieved 2026-05-29).
    # Output price includes thinking tokens.
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00, "cache_read": 0.15},
    "gemini-3-flash-preview": {"input": 0.50, "output": 3.00, "cache_read": 0.05},
    "gemini-3-pro-preview": {
        "tiers": [
            {"threshold": 200_000, "input": 2.00, "output": 12.00, "cache_read": 0.20},
            {"threshold": float("inf"), "input": 4.00, "output": 18.00, "cache_read": 0.40},
        ],
    },
    "gemini-3.1-pro-preview": {
        "tiers": [
            {"threshold": 200_000, "input": 2.00, "output": 12.00, "cache_read": 0.20},
            {"threshold": float("inf"), "input": 4.00, "output": 18.00, "cache_read": 0.40},
        ],
    },
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_read": 0.03},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "cache_read": 0.01},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "cache_read": 0.125},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30, "cache_read": 0.01875},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00, "cache_read": 0.3125},
    # ── Z.AI GLM ──────────────────────────────────────────────────────────
    "glm-5.1": {"input": 1.4, "output": 4.4, "cache_read": 0.26, "cache_write": 1.4},
    "glm-5": {"input": 1.0, "output": 3.2, "cache_read": 0.20, "cache_write": 1.0},
    "glm-5-turbo": {"input": 1.2, "output": 4.0, "cache_read": 0.24, "cache_write": 1.2},
    "glm-4.7": {"input": 0.5, "output": 2.0, "cache_read": 0.10, "cache_write": 0.5},
    "glm-4.5-air": {"input": 0.2, "output": 1.1, "cache_read": 0.03, "cache_write": 0.2},
    # ── Moonshot Kimi ─────────────────────────────────────────────────────
    "kimi-k2.5": {"input": 0.6, "output": 3.0, "cache_read": 0.1},
    "kimi-k2.6": {"input": 0.95, "output": 4.0, "cache_read": 0.16},
    # ── DeepSeek ──────────────────────────────────────────────────────────
    "deepseek-v4-pro": {"input": 0.435, "output": 0.87, "cache_read": 0.003625, "cache_write": 0.435},
    # ── MiniMax ───────────────────────────────────────────────────────────
    "minimax-2.5": {"input": 0.3, "output": 2.4, "cache_read": 0.03, "cache_write": 0.375},
}


# ---------------------------------------------------------------------------
# Prefix stripping for provider-qualified model IDs
# ---------------------------------------------------------------------------

_KNOWN_PREFIXES = (
    "litellm_proxy/",
    "gemini/",
    "openrouter/moonshotai/",
    "openrouter/minimax/",
    "openrouter/",
)


def _strip_prefix(model: str) -> str:
    """Strip known provider prefixes from a model ID."""
    for prefix in _KNOWN_PREFIXES:
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


# ---------------------------------------------------------------------------
# Fuzzy match order (substring → key), most specific first
# ---------------------------------------------------------------------------

# Only entries where pattern ≠ MODEL_PRICING key, or where the pattern is a
# substring used to match versioned model IDs (e.g. "claude-sonnet-4-6-20250514").
# Exact matches against MODEL_PRICING keys are handled in step 1 of resolve_pricing(),
# so they don't need to appear here.
_MATCH_ORDER = [
    # Anthropic family names (match any dated version)
    ("opus", "claude-opus"),
    ("haiku", "claude-haiku"),
    ("sonnet", "claude-sonnet"),
    # GPT (substring → key, only where pattern differs from key or for short aliases)
    ("gpt-5.5", "gpt-5.5"),  # matches "gpt-5.5-2026-04-23", "gpt-5.5-codex" etc.
    ("gpt-5.3-codex", "gpt-5.3-codex"),  # match "gpt-5.3-codex-xxxxx"
    ("gpt-5.2-codex", "gpt-5.2-codex"),
    ("gpt-5.2-pro", "gpt-5.2-pro"),
    ("gpt-4o-mini", "gpt-4o-mini"),       # must come before "gpt-4o"
    ("gpt-4o", "gpt-4o"),
    ("gpt-4-turbo", "gpt-4-turbo"),       # must come before "gpt-4"
    ("gpt-4", "gpt-4"),
    ("gpt-3.5", "gpt-3.5-turbo"),         # "gpt-3.5-turbo-0125" etc.
    # Gemini (short names → full key with "-preview" suffix)
    ("gemini-3.1-pro", "gemini-3.1-pro-preview"),
    ("gemini-3.5-flash", "gemini-3.5-flash"),  # must come before gemini-3-flash
    ("gemini-3-pro", "gemini-3-pro-preview"),
    ("gemini-3-flash", "gemini-3-flash-preview"),
    ("gemini-2.5-flash-lite", "gemini-2.5-flash-lite"),  # must come before "gemini-2.5-flash"
    ("gemini-2.5-flash", "gemini-2.5-flash"),
    ("gemini-2.5-pro", "gemini-2.5-pro"),
    ("gemini-1.5-flash", "gemini-1.5-flash"),
    ("gemini-1.5-pro", "gemini-1.5-pro"),
    # GLM (only needed for versioned suffixes like "glm-5-20260401")
    ("glm-5.1", "glm-5.1"),      # must come before "glm-5"
    ("glm-5-turbo", "glm-5-turbo"),  # must come before "glm-5"
    ("glm-5", "glm-5"),
    ("glm-4.5-air", "glm-4.5-air"),  # must come before "glm-4"
    ("glm-4.7", "glm-4.7"),
    # Kimi
    ("kimi-k2.6", "kimi-k2.6"),  # must come before "kimi-k2"
    ("kimi-k2", "kimi-k2.5"),    # "kimi-k2" or "kimi-k2.5-xxxxx"
    # DeepSeek
    ("deepseek-v4-pro", "deepseek-v4-pro"),
    # MiniMax
    ("minimax", "minimax-2.5"),   # "minimax-xxxxx" → minimax-2.5
]


# ---------------------------------------------------------------------------
# Resolve pricing
# ---------------------------------------------------------------------------

def _resolve_tiered(entry: Dict[str, Any], prompt_tokens: Optional[int] = None) -> Dict[str, float]:
    """Resolve a tiered pricing entry to a flat rate dict."""
    tiers: Optional[List[Dict]] = entry.get("tiers")
    if not tiers:
        return entry  # type: ignore[return-value]
    pt = prompt_tokens if prompt_tokens is not None else 0
    for tier in tiers:
        if tier["threshold"] == float("inf") or pt <= int(tier["threshold"]):
            return {k: v for k, v in tier.items() if k != "threshold"}
    return {k: v for k, v in tiers[-1].items() if k != "threshold"}


_DEFAULT_PRICING: Dict[str, float] = MODEL_PRICING["claude-sonnet"]  # type: ignore[assignment]


def resolve_pricing(
    model: str,
    prompt_tokens: Optional[int] = None,
) -> Dict[str, float]:
    """Resolve a model ID string to a flat pricing dict.

    Lookup strategy:
      1. Exact match in MODEL_PRICING
      2. Strip known provider prefixes, then exact match
      3. Substring match via _MATCH_ORDER
      4. Fallback to claude-sonnet

    For tiered models, *prompt_tokens* selects the pricing tier.

    Returns:
        Dict with at least ``input``, ``output``, ``cache_read``.
    """
    model_l = (model or "").lower()

    # 1. Exact match
    if model_l in MODEL_PRICING:
        return _resolve_tiered(MODEL_PRICING[model_l], prompt_tokens)

    # 2. Strip prefix, exact match
    stripped = _strip_prefix(model_l)
    if stripped != model_l and stripped in MODEL_PRICING:
        return _resolve_tiered(MODEL_PRICING[stripped], prompt_tokens)

    # 3. Substring match
    for pattern, key in _MATCH_ORDER:
        if pattern in model_l:
            return _resolve_tiered(MODEL_PRICING[key], prompt_tokens)

    return _DEFAULT_PRICING


def has_tiered_pricing(model: str) -> bool:
    """Check whether a model uses tiered pricing."""
    model_l = (model or "").lower()
    entry = MODEL_PRICING.get(model_l) or MODEL_PRICING.get(_strip_prefix(model_l))
    if entry:
        return "tiers" in entry
    for pattern, key in _MATCH_ORDER:
        if pattern in model_l:
            return "tiers" in MODEL_PRICING.get(key, {})
    return False


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------

def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
    prompt_tokens: Optional[int] = None,
) -> float:
    """Calculate cost in USD.

    Args:
        model: Model ID string.
        input_tokens: Non-cached input tokens.
        output_tokens: Output (completion) tokens.
        cache_read_tokens: Tokens read from prompt cache.
        cache_write_tokens: Tokens written to cache (generic, used when
            5m/1h breakdown is not available).
        cache_write_5m_tokens: 5-minute ephemeral cache writes (Claude only).
        cache_write_1h_tokens: 1-hour ephemeral cache writes (Claude only).
        prompt_tokens: Total prompt tokens (for tiered pricing resolution).

    When *cache_write_5m_tokens* or *cache_write_1h_tokens* are provided,
    they are priced at the model's ``cache_write_5m`` / ``cache_write_1h``
    rates. Otherwise *cache_write_tokens* uses the generic ``cache_write``
    rate (which falls back to ``input`` if absent).
    """
    p = resolve_pricing(model, prompt_tokens)
    cost = (
        input_tokens / 1_000_000 * p.get("input", 0)
        + output_tokens / 1_000_000 * p.get("output", 0)
        + cache_read_tokens / 1_000_000 * p.get("cache_read", 0)
    )
    # Use 5m/1h breakdown when available, otherwise generic cache_write
    if cache_write_5m_tokens or cache_write_1h_tokens:
        cost += cache_write_5m_tokens / 1_000_000 * p.get("cache_write_5m", p.get("cache_write", p.get("input", 0)))
        cost += cache_write_1h_tokens / 1_000_000 * p.get("cache_write_1h", p.get("cache_write", p.get("input", 0)))
    else:
        cost += cache_write_tokens / 1_000_000 * p.get("cache_write", p.get("input", 0))
    return cost


def is_non_claude_model(model: str) -> bool:
    """Check if a model is not a Claude model."""
    return not (model or "").lower().startswith("claude")


def calculate_cost_from_model_usage(model_usage: dict) -> Optional[float]:
    """Recalculate total cost from a modelUsage dict (as stored in agent_stats).

    Handles both Claude-style fields (``cacheReadInputTokens``,
    ``cacheCreationInputTokens``) and OpenAI-style (``cachedInputTokens``).

    Returns None if model_usage is empty.
    """
    if not model_usage:
        return None

    total = 0.0
    for model_id, usage in model_usage.items():
        if not isinstance(usage, dict):
            continue
        total += calculate_cost(
            model=model_id,
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
            cache_read_tokens=(
                usage.get("cacheReadInputTokens", 0)
                or usage.get("cachedInputTokens", 0)
            ),
            cache_write_tokens=usage.get("cacheCreationInputTokens", 0),
        )
    return total
