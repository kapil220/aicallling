"""Raw-REST client for the PLATFORM Twilio account (saas phase 3).

Mirrors TwilioProvider's aiohttp style (services/telephony/providers/twilio/
provider.py) rather than the twilio SDK. Subaccounts isolate each org's
numbers; purchase/release are executed as the subaccount.
"""

from functools import lru_cache
from typing import Optional

import aiohttp

from api.constants import (
    PLATFORM_TWILIO_ACCOUNT_SID,
    PLATFORM_TWILIO_AUTH_TOKEN,
)

_API = "https://api.twilio.com/2010-04-01"


class PlatformTwilioError(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


async def _check(resp) -> dict:
    if resp.status >= 300:
        try:
            payload = await resp.json()
            message = payload.get("message") or str(payload)
        except Exception:
            message = f"Twilio error (HTTP {resp.status})"
        raise PlatformTwilioError(message)
    if resp.status == 204:
        return {}
    return await resp.json()


class PlatformTwilioClient:
    def __init__(self, account_sid: str, auth_token: str):
        self._account_sid = account_sid
        self._auth = aiohttp.BasicAuth(account_sid, auth_token)

    async def search_available_numbers(
        self,
        *,
        country: str,
        area_code: Optional[str] = None,
        contains: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        url = (
            f"{_API}/Accounts/{self._account_sid}"
            f"/AvailablePhoneNumbers/{country}/Local.json"
        )
        params: dict = {"PageSize": limit}
        if area_code:
            params["AreaCode"] = area_code
        if contains:
            params["Contains"] = contains
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, auth=self._auth) as resp:
                payload = await _check(resp)
        return [
            {
                "phone_number": n.get("phone_number"),
                "friendly_name": n.get("friendly_name"),
                "locality": n.get("locality"),
                "region": n.get("region"),
            }
            for n in payload.get("available_phone_numbers", [])
        ]

    async def create_subaccount(self, friendly_name: str) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API}/Accounts.json",
                data={"FriendlyName": friendly_name},
                auth=self._auth,
            ) as resp:
                payload = await _check(resp)
        return {"sid": payload["sid"], "auth_token": payload["auth_token"]}

    async def purchase_number(
        self,
        *,
        subaccount_sid: str,
        subaccount_token: str,
        phone_number: str,
        voice_url: str,
    ) -> dict:
        sub_auth = aiohttp.BasicAuth(subaccount_sid, subaccount_token)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{_API}/Accounts/{subaccount_sid}/IncomingPhoneNumbers.json",
                data={
                    "PhoneNumber": phone_number,
                    "VoiceUrl": voice_url,
                    "VoiceMethod": "POST",
                },
                auth=sub_auth,
            ) as resp:
                payload = await _check(resp)
        return {"sid": payload["sid"], "phone_number": payload["phone_number"]}

    async def release_number(
        self, *, subaccount_sid: str, subaccount_token: str, number_sid: str
    ) -> None:
        sub_auth = aiohttp.BasicAuth(subaccount_sid, subaccount_token)
        async with aiohttp.ClientSession() as session:
            async with session.delete(
                f"{_API}/Accounts/{subaccount_sid}/IncomingPhoneNumbers/{number_sid}.json",
                auth=sub_auth,
            ) as resp:
                await _check(resp)


@lru_cache(maxsize=1)
def get_platform_twilio() -> PlatformTwilioClient:
    if not PLATFORM_TWILIO_ACCOUNT_SID or not PLATFORM_TWILIO_AUTH_TOKEN:
        raise PlatformTwilioError("platform_twilio_not_configured")
    return PlatformTwilioClient(PLATFORM_TWILIO_ACCOUNT_SID, PLATFORM_TWILIO_AUTH_TOKEN)
