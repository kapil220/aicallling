from api.services.billing.billing_service import affordable_cap


def test_cap_reduces_to_affordable():
    assert affordable_cap(300, {"max_affordable_seconds": 120}) == 120


def test_cap_keeps_configured_when_affordable_higher():
    assert affordable_cap(300, {"max_affordable_seconds": 100000}) == 300


def test_cap_noop_without_affordable():
    assert affordable_cap(300, {}) == 300
    assert affordable_cap(300, None) == 300
