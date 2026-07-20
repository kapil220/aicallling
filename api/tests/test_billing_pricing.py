from types import SimpleNamespace

from api.services.billing.pricing import ArchitectureKey, resolve_rate


def _rule(**kw):
    base = dict(
        id=1,
        organization_id=None,
        mode=None,
        llm_provider=None,
        stt_provider=None,
        tts_provider=None,
        realtime_provider=None,
        price_per_minute_cents=100,
        priority=0,
        is_active=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_most_specific_rule_wins():
    arch = ArchitectureKey("pipeline", "openai", "deepgram", "elevenlabs", None)
    rules = [
        _rule(id=1, price_per_minute_cents=100),  # global wildcard
        _rule(id=2, mode="pipeline", price_per_minute_cents=90),
        _rule(
            id=3,
            mode="pipeline",
            llm_provider="openai",
            stt_provider="deepgram",
            tts_provider="elevenlabs",
            price_per_minute_cents=70,
        ),
    ]
    res = resolve_rate(arch, rules, None, None)
    assert res.price_per_minute_cents == 70
    assert res.matched_rule_id == 3
    assert res.source == "rule"


def test_priority_breaks_specificity_tie():
    arch = ArchitectureKey("pipeline", "openai", "deepgram", "elevenlabs", None)
    rules = [
        _rule(id=1, mode="pipeline", price_per_minute_cents=90, priority=1),
        _rule(id=2, mode="pipeline", price_per_minute_cents=80, priority=5),
    ]
    res = resolve_rate(arch, rules, None, None)
    assert res.matched_rule_id == 2


def test_non_matching_rule_excluded():
    arch = ArchitectureKey("realtime", None, None, None, "openai_realtime")
    rules = [_rule(id=1, mode="pipeline", price_per_minute_cents=90)]
    res = resolve_rate(arch, rules, None, None)
    assert res.source == "none"


def test_org_fallback_used_when_no_rule():
    arch = ArchitectureKey("pipeline", "openai", "deepgram", "elevenlabs", None)
    res = resolve_rate(arch, [], 0.01, None)  # $0.01/s -> $0.60/min -> 60c
    assert res.price_per_minute_cents == 60
    assert res.source == "org_fallback"


def test_global_default_last_resort():
    arch = ArchitectureKey("pipeline", "openai", "deepgram", "elevenlabs", None)
    res = resolve_rate(arch, [], None, 50)
    assert res.price_per_minute_cents == 50
    assert res.source == "global_default"


def test_inactive_rule_ignored():
    arch = ArchitectureKey("pipeline", "openai", "deepgram", "elevenlabs", None)
    rules = [_rule(id=1, mode="pipeline", price_per_minute_cents=90, is_active=False)]
    res = resolve_rate(arch, rules, None, 50)
    assert res.source == "global_default"
