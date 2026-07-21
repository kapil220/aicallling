"""Seed a new org's model configuration from platform-held provider keys.

In saas mode tenants never supply AI provider keys (spec §7): every new org
gets a working default pipeline (OpenAI LLM + Deepgram STT + ElevenLabs TTS)
backed by the platform's own keys. Superadmin can change any org's config
later through the existing configuration endpoints.
"""

from loguru import logger

from api.constants import (
    PLATFORM_DEEPGRAM_API_KEY,
    PLATFORM_ELEVENLABS_API_KEY,
    PLATFORM_OPENAI_API_KEY,
)
from api.db import db_client
from api.enums import OrganizationConfigurationKey
from api.schemas.ai_model_configuration import (
    BYOKAIModelConfiguration,
    BYOKPipelineAIModelConfiguration,
    OrganizationAIModelConfigurationV2,
)
from api.services.configuration.registry import (
    DeepgramSTTConfiguration,
    ElevenlabsTTSConfiguration,
    OpenAILLMService,
)


def _build_default_v2() -> OrganizationAIModelConfigurationV2 | None:
    if not (
        PLATFORM_OPENAI_API_KEY
        and PLATFORM_DEEPGRAM_API_KEY
        and PLATFORM_ELEVENLABS_API_KEY
    ):
        return None
    pipeline = BYOKPipelineAIModelConfiguration(
        llm=OpenAILLMService(api_key=[PLATFORM_OPENAI_API_KEY]),
        stt=DeepgramSTTConfiguration(api_key=[PLATFORM_DEEPGRAM_API_KEY]),
        tts=ElevenlabsTTSConfiguration(api_key=[PLATFORM_ELEVENLABS_API_KEY]),
    )
    return OrganizationAIModelConfigurationV2(
        mode="byok",
        byok=BYOKAIModelConfiguration(mode="pipeline", pipeline=pipeline),
    )


async def seed_platform_model_configuration(organization_id: int) -> bool:
    """Write a default MODEL_CONFIGURATION_V2 for `organization_id` from platform keys.

    Returns True if a configuration was written, False (with a warning logged)
    if the platform doesn't have all three provider keys configured.
    """
    config = _build_default_v2()
    if config is None:
        logger.warning(
            "No platform AI keys configured; org {} starts without a default "
            "model configuration",
            organization_id,
        )
        return False
    await db_client.upsert_configuration(
        organization_id,
        OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value,
        config.model_dump(mode="json", exclude_none=True),
    )
    return True
