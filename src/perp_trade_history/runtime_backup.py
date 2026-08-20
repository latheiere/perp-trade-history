"""Signed backup and verification for collector-owned durable state."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from statecrate import (
    Limits,
    SigningKey,
    Source,
    VerificationKey,
    backup,
    restore,
    verify,
)

APPLICATION = "perp-trade-history"
SIGNING_KEY_NAME = "statecrate-signing-key.pem"
VERIFICATION_KEY_NAME = "statecrate-verification-key.pem"
LIMITS = Limits(max_members=100_000, max_expanded_bytes=2 * 1024 * 1024 * 1024)


def create_backup(*, output: Path, data_dir: Path) -> dict[str, Any]:
    archive = output.expanduser().absolute()
    state = data_dir.expanduser().resolve()
    if archive == state or archive.is_relative_to(state):
        raise ValueError("backup archive must be outside the durable data directory")
    signing_key = _load_or_create_key(archive.parent)
    report = backup(
        archive,
        [Source.directory(state, at="trade-history")],
        signing_key=signing_key,
        limits=LIMITS,
        metadata={"application": APPLICATION, "state_schema": 1},
    )
    return {
        "ok": True,
        "archive": str(report.archive),
        "created_at": report.created_at,
        "entries": report.entries,
        "expanded_bytes": report.expanded_bytes,
        "signer_key_id": report.signer_key_id,
    }


def verify_backup(*, archive: Path, verification_key: Path | None = None) -> dict[str, Any]:
    path = archive.expanduser().absolute()
    key_path = verification_key or path.parent / VERIFICATION_KEY_NAME
    report = verify(
        path,
        trusted_keys=VerificationKey.load(key_path),
        limits=LIMITS,
    )
    metadata = dict(report.metadata)
    if metadata != {"application": APPLICATION, "state_schema": 1}:
        raise ValueError("backup metadata does not identify compatible collector state")
    return {
        "ok": True,
        "archive": str(report.archive),
        "created_at": report.created_at,
        "entries_checked": report.entries_checked,
        "expanded_bytes": report.expanded_bytes,
        "signer_key_id": report.signer_key_id,
    }


def restore_backup(
    *,
    archive: Path,
    target: Path,
    verification_key: Path | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    verified = verify_backup(
        archive=archive, verification_key=verification_key
    )
    path = archive.expanduser().absolute()
    key_path = verification_key or path.parent / VERIFICATION_KEY_NAME
    report = restore(
        path,
        target,
        trusted_keys=VerificationKey.load(key_path),
        limits=LIMITS,
        replace=replace,
    )
    return {
        "ok": True,
        "archive": str(report.archive),
        "restored_to": str(report.destination),
        "entries_restored": report.restored_entries,
        "expanded_bytes": report.expanded_bytes,
        "signer_key_id": verified["signer_key_id"],
    }


def _load_or_create_key(directory: Path) -> SigningKey:
    signing_path = directory / SIGNING_KEY_NAME
    verification_path = directory / VERIFICATION_KEY_NAME
    if not signing_path.exists() and not verification_path.exists():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = SigningKey.generate()
        key.save(signing_path)
        key.verification_key.save(verification_path)
        return key
    if not signing_path.is_file() or not verification_path.is_file():
        raise ValueError("Statecrate signing and verification keys must both exist")
    key = SigningKey.load(signing_path)
    if key.key_id != VerificationKey.load(verification_path).key_id:
        raise ValueError("Statecrate signing and verification keys do not match")
    return key
