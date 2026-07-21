"""Tests for platform-key default model configuration seeding (saas mode).

Uses the savepoint-isolated `db_session` fixture (see api/conftest.py) so
each test's org + configuration rows are rolled back automatically.
"""

import pytest

from api.enums import OrganizationConfigurationKey


@pytest.mark.asyncio
async def test_seed_writes_v2_config(db_session, monkeypatch):
    from api.services.configuration import platform_defaults

    monkeypatch.setattr(platform_defaults, "PLATFORM_OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(platform_defaults, "PLATFORM_DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setattr(platform_defaults, "PLATFORM_ELEVENLABS_API_KEY", "el-test")

    org, _ = await db_session.get_or_create_organization_by_provider_id(
        org_provider_id="org_platform_seed", user_id=None
    )
    assert await platform_defaults.seed_platform_model_configuration(org.id) is True

    stored = await db_session.get_configuration(
        org.id, OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value
    )
    assert stored is not None
    # Keys are stored server-side; assert they landed in the config blob.
    assert "sk-test" in str(stored.value)
    assert "dg-test" in str(stored.value)
    assert "el-test" in str(stored.value)


@pytest.mark.asyncio
async def test_seed_skips_without_keys(db_session, monkeypatch):
    from api.services.configuration import platform_defaults

    monkeypatch.setattr(platform_defaults, "PLATFORM_OPENAI_API_KEY", None)
    monkeypatch.setattr(platform_defaults, "PLATFORM_DEEPGRAM_API_KEY", None)
    monkeypatch.setattr(platform_defaults, "PLATFORM_ELEVENLABS_API_KEY", None)

    org, _ = await db_session.get_or_create_organization_by_provider_id(
        org_provider_id="org_platform_seed_none", user_id=None
    )
    assert await platform_defaults.seed_platform_model_configuration(org.id) is False

    stored = await db_session.get_configuration(
        org.id, OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value
    )
    assert stored is None
