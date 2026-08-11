"""Schema checks for phase-3 platform-number columns (migration b5f50d0de0005)."""

from api.db.models import TelephonyConfigurationModel, TelephonyPhoneNumberModel


async def test_new_columns_present(db_session):
    config_cols = TelephonyConfigurationModel.__table__.columns
    assert "is_platform" in config_cols
    assert config_cols["is_platform"].default.arg is False

    phone_cols = TelephonyPhoneNumberModel.__table__.columns
    for name in (
        "is_platform",
        "provider_sid",
        "rental_cents",
        "next_renewal_at",
        "rental_status",
    ):
        assert name in phone_cols, name
    assert phone_cols["is_platform"].default.arg is False
