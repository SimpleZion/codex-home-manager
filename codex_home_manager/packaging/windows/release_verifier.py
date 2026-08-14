from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from artifact_verifier_core import (
    VerificationError,
    load_verified_manifest,
    select_artifact_record,
    verify_artifact,
)


trusted_public_key_fingerprint = "sha256:ef7194fbc8fa8550430c908d9d02c74f7fc0d1e87f7f9b4ec5a164526b48f208"
default_metadata_base_url = "https://codex-home-manager.simplezion.com"
metadata_names = (
    "release-manifest.json",
    "release-manifest.json.sig",
    "release-signing-public-key.pem",
)


def copy_metadata(*, name: str, destination: Path, metadata_directory: Path | None, metadata_base_url: str) -> None:
    if metadata_directory is not None:
        source = metadata_directory.resolve() / name
        if not source.is_file():
            raise VerificationError(f"verification metadata file not found: {source}")
        shutil.copyfile(source, destination)
        return
    url = f"{metadata_base_url.rstrip('/')}/{name}"
    request = Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "CodexHomeManagerVerifier/1"})
    try:
        with urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise VerificationError(f"verification metadata request returned HTTP {response.status}: {url}")
            destination.write_bytes(response.read())
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise VerificationError(f"verification metadata download failed: {url}: {error}") from error


def resolve_artifact_path(
    *, requested_path: Path | None, artifact_kind: str, manifest_path: Path, signature_path: Path, public_key_path: Path
) -> Path:
    if requested_path is not None:
        return requested_path.expanduser().resolve()
    manifest, _ = load_verified_manifest(
        manifest_path=manifest_path,
        signature_path=signature_path,
        public_key_path=public_key_path,
        trusted_fingerprint_value=trusted_public_key_fingerprint,
    )
    record = select_artifact_record(manifest, artifact_kind)
    artifact_name = record["path"]
    if not isinstance(artifact_name, str):
        raise VerificationError("signed artifact name is invalid")
    downloads = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Downloads"
    return (downloads / artifact_name).resolve()


def run_verification(arguments: argparse.Namespace) -> dict[str, object]:
    metadata_directory = arguments.metadata_directory.resolve() if arguments.metadata_directory else None
    with tempfile.TemporaryDirectory(prefix="codex-home-manager-verifier-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        metadata_paths = {name: temporary_root / name for name in metadata_names}
        for name, destination in metadata_paths.items():
            copy_metadata(
                name=name,
                destination=destination,
                metadata_directory=metadata_directory,
                metadata_base_url=arguments.metadata_base_url,
            )
        artifact_path = resolve_artifact_path(
            requested_path=arguments.file_path,
            artifact_kind=arguments.artifact_kind,
            manifest_path=metadata_paths["release-manifest.json"],
            signature_path=metadata_paths["release-manifest.json.sig"],
            public_key_path=metadata_paths["release-signing-public-key.pem"],
        )
        verification_arguments = argparse.Namespace(
            manifest=metadata_paths["release-manifest.json"],
            signature=metadata_paths["release-manifest.json.sig"],
            public_key=metadata_paths["release-signing-public-key.pem"],
            trusted_public_key_fingerprint=trusted_public_key_fingerprint,
            artifact_kind=arguments.artifact_kind,
            artifact=artifact_path,
        )
        return verify_artifact(verification_arguments)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify Codex Home Manager without requiring Python or additional packages."
    )
    parser.add_argument("--file-path", type=Path, help="Downloaded connector EXE or ZIP. Defaults to the signed name in Downloads.")
    parser.add_argument("--artifact-kind", choices=("exe", "zip"), default="exe")
    parser.add_argument("--metadata-base-url", default=default_metadata_base_url)
    parser.add_argument("--metadata-directory", type=Path, help="Use local signed metadata instead of downloading it.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser


def main() -> int:
    try:
        result = run_verification(build_parser().parse_args())
    except VerificationError as error:
        print(f"VERIFICATION_FAILED: {error}", file=sys.stderr)
        return 1
    if "--json" in sys.argv:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        print("OK: release signature, pinned signing key, artifact size, and SHA-256 all match.")
        print(f"Artifact: {result['manifest_name']}")
        print(f"SHA-256: {result['sha256']}")
        print(f"Pinned key: {result['public_key_fingerprint']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
