from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import sys
from pathlib import Path

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError as error:
    raise RuntimeError(
        "DEPENDENCY_UNAVAILABLE: install the Python 'cryptography' package or use the self-contained Windows verifier"
    ) from error


class VerificationError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_fingerprint(value: str) -> str:
    normalized = value.strip().lower()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", normalized) is None:
        raise VerificationError("the embedded trusted public-key fingerprint is invalid")
    return normalized


def select_artifact_record(manifest: dict[str, object], artifact_kind: str) -> dict[str, object]:
    records = manifest.get("public_artifacts")
    if not isinstance(records, list):
        raise VerificationError("signed release manifest has no public artifact records")
    expected_suffix = f".{artifact_kind}"
    name_pattern = re.compile(
        rf"codex-home-manager-local-win-x64-v[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{{12}}\{expected_suffix}"
    )
    candidates = [
        record
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("path"), str)
        and name_pattern.fullmatch(record["path"]) is not None
    ]
    if len(candidates) != 1:
        raise VerificationError(f"signed release manifest must contain exactly one {artifact_kind.upper()} artifact")
    return candidates[0]


def load_verified_manifest(
    *, manifest_path: Path, signature_path: Path, public_key_path: Path, trusted_fingerprint_value: str
) -> tuple[dict[str, object], str]:
    trusted_fingerprint = normalize_fingerprint(trusted_fingerprint_value)
    try:
        public_key_bytes = public_key_path.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        signature_text = signature_path.read_text(encoding="ascii").strip()
    except OSError as error:
        raise VerificationError(f"release verification metadata is unreadable: {error}") from error

    try:
        public_key = serialization.load_pem_public_key(public_key_bytes)
    except ValueError as error:
        raise VerificationError("release public key PEM is invalid") from error
    if not isinstance(public_key, Ed25519PublicKey):
        raise VerificationError("release public key is not Ed25519")
    spki_der = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    actual_fingerprint = f"sha256:{hashlib.sha256(spki_der).hexdigest()}"
    if not hmac.compare_digest(actual_fingerprint, trusted_fingerprint):
        raise VerificationError("release public key SPKI SHA-256 does not match the embedded trust anchor")

    try:
        signature_bytes = base64.b64decode(signature_text, validate=True)
        public_key.verify(signature_bytes, manifest_bytes)
    except (ValueError, InvalidSignature) as error:
        raise VerificationError("release manifest Ed25519 signature verification failed") from error

    # The manifest is untrusted input until the detached signature above succeeds.
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerificationError("signed release manifest is not valid UTF-8 JSON") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 5:
        raise VerificationError("signed release manifest has an unsupported schema")
    if manifest.get("public_key_fingerprint") != trusted_fingerprint:
        raise VerificationError("signed release manifest public-key fingerprint does not match the trust anchor")
    return manifest, trusted_fingerprint


def verify_artifact(arguments: argparse.Namespace) -> dict[str, object]:
    manifest, trusted_fingerprint = load_verified_manifest(
        manifest_path=arguments.manifest,
        signature_path=arguments.signature,
        public_key_path=arguments.public_key,
        trusted_fingerprint_value=arguments.trusted_public_key_fingerprint,
    )
    record = select_artifact_record(manifest, arguments.artifact_kind)
    expected_hash = record.get("sha256")
    expected_size = record.get("size")
    if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
        raise VerificationError("signed artifact record has an invalid SHA-256")
    if not isinstance(expected_size, int) or isinstance(expected_size, bool) or expected_size < 1:
        raise VerificationError("signed artifact record has an invalid size")
    if not arguments.artifact.is_file():
        raise VerificationError(f"downloaded artifact does not exist: {arguments.artifact}")
    actual_size = arguments.artifact.stat().st_size
    if actual_size != expected_size:
        raise VerificationError(f"downloaded artifact size mismatch: expected {expected_size}, got {actual_size}")
    actual_hash = sha256_file(arguments.artifact)
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise VerificationError(f"downloaded artifact SHA-256 mismatch: expected {expected_hash}, got {actual_hash}")
    return {
        "artifact": str(arguments.artifact),
        "manifest_name": record["path"],
        "sha256": expected_hash,
        "size": expected_size,
        "public_key_fingerprint": trusted_fingerprint,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify one Codex Home Manager artifact against a signed release manifest.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--signature", required=True, type=Path)
    parser.add_argument("--public-key", required=True, type=Path)
    parser.add_argument("--trusted-public-key-fingerprint", required=True)
    parser.add_argument("--artifact-kind", required=True, choices=("exe", "zip"))
    parser.add_argument("--artifact", required=True, type=Path)
    try:
        result = verify_artifact(parser.parse_args())
    except VerificationError as error:
        print(f"VERIFICATION_FAILED: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
