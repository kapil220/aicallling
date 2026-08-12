from typing import Any, Dict, List, Optional

from sqlalchemy.future import select

from api.db.base_client import BaseDBClient
from api.db.models import OrganizationConfigurationModel
from api.enums import OrganizationConfigurationKey
from api.services.security import credential_crypto

# ``organization_configurations.value`` is a mixed bag keyed by ``key``: only
# these four carry secrets. The rest — disposition mappings, call limits, org
# preferences — are ordinary configuration and stay readable in the database
# for operations and debugging.
#
# MODEL_CONFIGURATION_V2 matters most: seed_platform_model_configuration copies
# the platform's own OpenAI/Deepgram/ElevenLabs keys into *every* org's row, so
# it is the highest-blast-radius secret we store.
#
# This is handled here rather than by EncryptedJSON because the decision
# depends on the sibling ``key`` column, which a TypeDecorator never sees.
SECRET_CONFIG_KEYS = frozenset(
    {
        OrganizationConfigurationKey.TELEPHONY_CONFIGURATION.value,
        OrganizationConfigurationKey.TWILIO_CONFIGURATION.value,
        OrganizationConfigurationKey.LANGFUSE_CREDENTIALS.value,
        OrganizationConfigurationKey.MODEL_CONFIGURATION_V2.value,
    }
)


def _encrypt_for_key(key: str, value: Any) -> Any:
    """Encrypt only the key types that hold secrets."""
    if key not in SECRET_CONFIG_KEYS:
        return value
    return credential_crypto.encrypt_json(value)


def _decrypted_value(config: Optional[OrganizationConfigurationModel]) -> Any:
    """Plaintext value of a row, whatever its storage state.

    Content-sniffed, so rows mid-backfill and rows in a deployment with no key
    configured both read correctly.
    """
    if config is None:
        return None
    return credential_crypto.decrypt_json(config.value)


def _decrypt_in_place(
    config: OrganizationConfigurationModel,
) -> OrganizationConfigurationModel:
    """Swap a detached row's ciphertext for plaintext before it leaves the client.

    Callers receive the ORM object itself, so decrypting on the way out is what
    keeps encryption invisible to every consumer of ``.value``.
    """
    config.value = credential_crypto.decrypt_json(config.value)
    return config


def _decrypt_optional(
    config: Optional[OrganizationConfigurationModel],
) -> Optional[OrganizationConfigurationModel]:
    return _decrypt_in_place(config) if config is not None else None


class OrganizationConfigurationClient(BaseDBClient):
    async def get_configuration(
        self, organization_id: int, key: str
    ) -> Optional[OrganizationConfigurationModel]:
        """Get a specific configuration for an organization by key."""
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationConfigurationModel).where(
                    OrganizationConfigurationModel.organization_id == organization_id,
                    OrganizationConfigurationModel.key == key,
                )
            )
            config = result.scalars().first()
        return _decrypt_optional(config)

    async def get_all_configurations(
        self, organization_id: int
    ) -> list[OrganizationConfigurationModel]:
        """Get all configurations for an organization."""
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationConfigurationModel).where(
                    OrganizationConfigurationModel.organization_id == organization_id
                )
            )
            configs = list(result.scalars().all())
        return [_decrypt_in_place(c) for c in configs]

    async def upsert_configuration(
        self, organization_id: int, key: str, value: Any
    ) -> OrganizationConfigurationModel:
        """Create or update a configuration for an organization."""
        async with self.async_session() as session:
            # First try to get existing configuration
            result = await session.execute(
                select(OrganizationConfigurationModel).where(
                    OrganizationConfigurationModel.organization_id == organization_id,
                    OrganizationConfigurationModel.key == key,
                )
            )
            config = result.scalars().first()

            stored = _encrypt_for_key(key, value)
            if config:
                # Update existing configuration
                config.value = stored
            else:
                # Create new configuration
                config = OrganizationConfigurationModel(
                    organization_id=organization_id,
                    key=key,
                    value=stored,
                )
                session.add(config)

            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                raise e
            await session.refresh(config)
        # Hand back the plaintext the caller passed in, not what we stored.
        return _decrypt_in_place(config)

    async def delete_configuration(self, organization_id: int, key: str) -> bool:
        """Delete a configuration for an organization."""
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationConfigurationModel).where(
                    OrganizationConfigurationModel.organization_id == organization_id,
                    OrganizationConfigurationModel.key == key,
                )
            )
            config = result.scalars().first()

            if not config:
                return False

            await session.delete(config)
            try:
                await session.commit()
            except Exception as e:
                await session.rollback()
                raise e
            return True

    async def get_configuration_value(
        self, organization_id: int, key: str, default: Any = None
    ) -> Any:
        """Get the value of a configuration, returning default if not found."""
        # get_configuration has already decrypted.
        config = await self.get_configuration(organization_id, key)
        return config.value if config else default

    async def get_all_configurations_by_key(self, key: str) -> list[dict[str, Any]]:
        """Get all organization configurations for a given key.

        Returns a list of dicts with organization_id and the config value.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationConfigurationModel).where(
                    OrganizationConfigurationModel.key == key,
                )
            )
            rows = [
                {
                    "organization_id": config.organization_id,
                    "value": _decrypted_value(config),
                }
                for config in result.scalars().all()
            ]
            return [row for row in rows if row["value"]]

    async def get_configurations_by_provider(
        self, key: str, provider: str
    ) -> List[Dict[str, Any]]:
        """Get all organization configurations for a given key filtered by provider.

        Returns a list of dicts with organization_id and the config value.
        """
        async with self.async_session() as session:
            result = await session.execute(
                select(OrganizationConfigurationModel).where(
                    OrganizationConfigurationModel.key == key,
                )
            )
            configs = result.scalars().all()

            # The provider filter has to run on plaintext — once encrypted this
            # value is an opaque token with no readable "provider" field.
            rows = [
                {
                    "organization_id": config.organization_id,
                    "value": _decrypted_value(config),
                }
                for config in configs
            ]
            return [
                row
                for row in rows
                if row["value"] and row["value"].get("provider") == provider
            ]
