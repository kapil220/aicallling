"""PlatformTwilioClient — aiohttp fully mocked; asserts URLs, auth, payloads."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.services.telephony import platform_twilio
from api.services.telephony.platform_twilio import (
    PlatformTwilioClient,
    PlatformTwilioError,
)


def _mock_response(status=200, payload=None):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload or {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(resp):
    session = MagicMock()
    for verb in ("get", "post", "delete"):
        getattr(session, verb).return_value = resp
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.fixture
def client():
    return PlatformTwilioClient("AC_platform", "platform_token")


async def test_search_builds_url_and_parses(client):
    resp = _mock_response(
        payload={
            "available_phone_numbers": [
                {
                    "phone_number": "+14155551234",
                    "friendly_name": "(415) 555-1234",
                    "locality": "San Francisco",
                    "region": "CA",
                }
            ]
        }
    )
    session = _mock_session(resp)
    with patch.object(platform_twilio.aiohttp, "ClientSession", return_value=session):
        results = await client.search_available_numbers(
            country="US", area_code="415", limit=5
        )
    assert results == [
        {
            "phone_number": "+14155551234",
            "friendly_name": "(415) 555-1234",
            "locality": "San Francisco",
            "region": "CA",
        }
    ]
    url = session.get.call_args.args[0]
    assert "Accounts/AC_platform/AvailablePhoneNumbers/US/Local.json" in url
    params = session.get.call_args.kwargs["params"]
    assert params["AreaCode"] == "415"
    assert params["PageSize"] == 5


async def test_create_subaccount(client):
    resp = _mock_response(payload={"sid": "AC_sub", "auth_token": "sub_tok"})
    session = _mock_session(resp)
    with patch.object(platform_twilio.aiohttp, "ClientSession", return_value=session):
        sub = await client.create_subaccount("voxagent-org-42")
    assert sub == {"sid": "AC_sub", "auth_token": "sub_tok"}
    assert session.post.call_args.args[0].endswith("/Accounts.json")
    assert session.post.call_args.kwargs["data"]["FriendlyName"] == "voxagent-org-42"


async def test_purchase_number_sets_voice_url(client):
    resp = _mock_response(
        status=201, payload={"sid": "PN_1", "phone_number": "+14155551234"}
    )
    session = _mock_session(resp)
    with patch.object(platform_twilio.aiohttp, "ClientSession", return_value=session):
        result = await client.purchase_number(
            subaccount_sid="AC_sub",
            subaccount_token="sub_tok",
            phone_number="+14155551234",
            voice_url="https://api.example.com/api/v1/telephony/inbound/run",
        )
    assert result == {"sid": "PN_1", "phone_number": "+14155551234"}
    assert "Accounts/AC_sub/IncomingPhoneNumbers.json" in session.post.call_args.args[0]
    data = session.post.call_args.kwargs["data"]
    assert data["PhoneNumber"] == "+14155551234"
    assert data["VoiceUrl"].endswith("/inbound/run")
    assert data["VoiceMethod"] == "POST"


async def test_twilio_error_surfaces_message(client):
    resp = _mock_response(
        status=400, payload={"message": "Regulatory bundle required for IN"}
    )
    session = _mock_session(resp)
    with patch.object(platform_twilio.aiohttp, "ClientSession", return_value=session):
        with pytest.raises(PlatformTwilioError) as err:
            await client.search_available_numbers(country="IN")
    assert "Regulatory bundle" in str(err.value)


async def test_release_number(client):
    resp = _mock_response(status=204)
    session = _mock_session(resp)
    with patch.object(platform_twilio.aiohttp, "ClientSession", return_value=session):
        await client.release_number(
            subaccount_sid="AC_sub", subaccount_token="sub_tok", number_sid="PN_1"
        )
    assert "IncomingPhoneNumbers/PN_1.json" in session.delete.call_args.args[0]


def test_factory_requires_env(monkeypatch):
    monkeypatch.setattr(platform_twilio, "PLATFORM_TWILIO_ACCOUNT_SID", None)
    platform_twilio.get_platform_twilio.cache_clear()
    with pytest.raises(PlatformTwilioError):
        platform_twilio.get_platform_twilio()
    platform_twilio.get_platform_twilio.cache_clear()
