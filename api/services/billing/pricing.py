"""Pure per-architecture pricing resolution for the local billing engine."""

from dataclasses import dataclass

_MATCH_FIELDS = (
    "mode",
    "llm_provider",
    "stt_provider",
    "tts_provider",
    "realtime_provider",
)


@dataclass(frozen=True)
class ArchitectureKey:
    mode: str | None = None
    llm_provider: str | None = None
    stt_provider: str | None = None
    tts_provider: str | None = None
    realtime_provider: str | None = None


@dataclass(frozen=True)
class RateResult:
    price_per_minute_cents: int
    matched_rule_id: int | None
    source: str  # rule | org_fallback | global_default | none


def _rule_matches(rule, arch: ArchitectureKey) -> bool:
    for field in _MATCH_FIELDS:
        rule_val = getattr(rule, field)
        if rule_val is not None and rule_val != getattr(arch, field):
            return False
    return True


def _specificity(rule) -> int:
    return sum(1 for field in _MATCH_FIELDS if getattr(rule, field) is not None)


def resolve_rate(
    arch: ArchitectureKey,
    rules: list,
    org_price_per_second_usd: float | None,
    global_default_cents: int | None,
) -> RateResult:
    """Resolve a per-minute rate for an architecture.

    Most-specific matching rule wins (a null rule field is a wildcard); ties break
    on higher ``priority`` then higher ``id``. Falls back to the org's per-second
    rate, then a global default, then a zero "none" result.
    """
    candidates = [
        r for r in rules if getattr(r, "is_active", True) and _rule_matches(r, arch)
    ]
    if candidates:
        best = max(candidates, key=lambda r: (_specificity(r), r.priority, r.id))
        return RateResult(int(best.price_per_minute_cents), best.id, "rule")
    if org_price_per_second_usd:
        return RateResult(round(org_price_per_second_usd * 60 * 100), None, "org_fallback")
    if global_default_cents is not None:
        return RateResult(int(global_default_cents), None, "global_default")
    return RateResult(0, None, "none")
