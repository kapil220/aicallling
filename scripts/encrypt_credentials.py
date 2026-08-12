"""Backfill credential encryption at rest (phase 4a).

Run from the repo root with the api environment sourced:

    python -m scripts.encrypt_credentials                    # dry run, the default
    python -m scripts.encrypt_credentials --apply
    python -m scripts.encrypt_credentials --verify
    python -m scripts.encrypt_credentials --decrypt --apply  # escape hatch

Deliberately a script and not an Alembic data migration: Alembic runs inside
deploy automation where the key may be absent, a failure there leaves a
half-converted table, and the downgrade path would itself need the key.

Every row is tested before it is touched, so an interrupted run resumes rather
than double-encrypting, and a table holding both plaintext and ciphertext is a
supported state throughout. Batches commit as they go — an interruption leaves
work done, not an hour discarded.

The one unrecoverable failure this cannot protect against is losing every key
while encrypted data exists. Back the keys up somewhere other than the machine
holding the database before running with --apply.
"""

import argparse
import asyncio
import json
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.constants import DATABASE_URL
from api.db.organization_configuration_client import SECRET_CONFIG_KEYS
from api.services.security import credential_crypto


# ``credentials``, ``connection_details`` and ``credential_data`` are mapped as
# EncryptedJSON, so the ORM would encrypt them on write and decrypt them on
# read — which would make this script unable to see what is actually stored.
# Every read and write below therefore goes through raw SQL against the JSON
# column, bypassing the type decorator entirely.
TARGETS: Dict[str, Tuple[str, str, Optional[Callable[[str], bool]]]] = {
    # name: (table, column, key_predicate on the sibling ``key`` column)
    "telephony_configurations": ("telephony_configurations", "credentials", None),
    "integrations": ("integrations", "connection_details", None),
    "external_credentials": ("external_credentials", "credential_data", None),
    "organization_configurations": (
        "organization_configurations",
        "value",
        lambda key: key in SECRET_CONFIG_KEYS,
    ),
}


def _session_factory():
    engine = create_async_engine(DATABASE_URL, echo=False)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _fetch_rows(session, table: str, column: str, has_key: bool):
    cols = f"id, {column}" + (", key" if has_key else "")
    result = await session.execute(text(f"SELECT {cols} FROM {table} ORDER BY id"))
    return result.fetchall()


async def _write_value(session, table: str, column: str, row_id: int, value: Any):
    # Cast through ::json so a Fernet token is stored as a JSON string, exactly
    # as EncryptedJSON writes it.
    await session.execute(
        text(f"UPDATE {table} SET {column} = CAST(:v AS json) WHERE id = :id"),
        {"v": json.dumps(value), "id": row_id},
    )


async def _process_table(
    session_maker,
    target_name: str,
    *,
    apply_changes: bool,
    decrypt: bool,
    batch_size: int,
) -> Tuple[int, int, int]:
    """Returns (eligible, converted, already_in_target_state)."""
    table, column, key_predicate = TARGETS[target_name]
    has_key = key_predicate is not None

    _, session_maker_ = session_maker
    eligible = already = 0

    async with session_maker_() as session:
        rows = await _fetch_rows(session, table, column, has_key)

    pending: List[Tuple[int, Any]] = []
    for row in rows:
        row_id = row[0]
        value = row[1]
        if key_predicate is not None and not key_predicate(row[2]):
            continue
        if value is None:
            continue

        eligible += 1
        encrypted_now = credential_crypto.is_encrypted(value)

        if decrypt:
            if not encrypted_now:
                already += 1
                continue
            pending.append((row_id, credential_crypto.decrypt_json(value)))
        else:
            if encrypted_now:
                already += 1
                continue
            pending.append((row_id, credential_crypto.encrypt_json(value)))

    converted = len(pending)

    if apply_changes and pending:
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            async with session_maker_() as session:
                for row_id, new_value in batch:
                    await _write_value(session, table, column, row_id, new_value)
                await session.commit()

    return eligible, converted, already


async def _verify(session_maker) -> int:
    """Report plaintext vs encrypted per target. Exit code 1 if plaintext remains."""
    _, session_maker_ = session_maker
    remaining = 0

    for name, (table, column, key_predicate) in TARGETS.items():
        has_key = key_predicate is not None
        async with session_maker_() as session:
            rows = await _fetch_rows(session, table, column, has_key)

        plaintext = encrypted = 0
        for row in rows:
            value = row[1]
            if key_predicate is not None and not key_predicate(row[2]):
                continue
            if value is None:
                continue
            if credential_crypto.is_encrypted(value):
                encrypted += 1
            else:
                plaintext += 1

        remaining += plaintext
        print(f"{name}: {encrypted} encrypted, {plaintext} plaintext")

    if remaining:
        print(f"\n{remaining} plaintext credential row(s) remain.")
    else:
        print("\nAll credential rows are encrypted.")
    return 1 if remaining else 0


async def main_async(args) -> int:
    if not credential_crypto.encryption_enabled():
        print(
            "CREDENTIAL_ENCRYPTION_KEYS is not set — nothing to do. Set it to "
            "the key this data should be encrypted with and re-run.",
            file=sys.stderr,
        )
        return 1

    engine, maker = _session_factory()
    session_maker = (engine, maker)

    try:
        if args.verify:
            return await _verify(session_maker)

        names = [args.table] if args.table else list(TARGETS)
        verb = "Decrypting" if args.decrypt else "Encrypting"
        mode = "APPLY" if args.apply else "DRY RUN (nothing is written)"
        print(f"{verb} credentials — {mode}\n")

        total = 0
        for name in names:
            eligible, converted, already = await _process_table(
                session_maker,
                name,
                apply_changes=args.apply,
                decrypt=args.decrypt,
                batch_size=args.batch_size,
            )
            total += converted
            print(
                f"{name}: {eligible} eligible, {converted} to convert, "
                f"{already} already in target state"
            )

        if not args.apply:
            print(f"\nDry run: {total} row(s) would be converted. Re-run with --apply.")
        else:
            print(f"\nConverted {total} row(s).")
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform writes (without this the script only reports)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit no-op form of the default behaviour",
    )
    parser.add_argument(
        "--table",
        choices=sorted(TARGETS),
        help="convert one table instead of all of them",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="report plaintext vs encrypted counts; exit 1 if plaintext remains",
    )
    parser.add_argument(
        "--decrypt",
        action="store_true",
        help="inverse conversion, for escape-hatch use while a key is available",
    )
    args = parser.parse_args()

    if args.dry_run:
        args.apply = False

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
