[CmdletBinding()]
param(
    [string]$FilePath = "",
    [ValidateSet("exe", "zip")]
    [string]$ArtifactKind = "exe",
    [ValidatePattern('^https://[^?#]+$')]
    [string]$MetadataBaseUrl = "https://codex-home-manager.simplezion.com",
    [string]$MetadataDirectory = ""
)

$ErrorActionPreference = "Stop"
$trustedPublicKeyFingerprint = "sha256:ef7194fbc8fa8550430c908d9d02c74f7fc0d1e87f7f9b4ec5a164526b48f208"
$defaultArtifactName = "codex-home-manager-local-win-x64-v1.0.9-fa74e7215289.exe"
$pythonVerifierSource = @'
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
except ImportError:
    print(
        "DEPENDENCY_UNAVAILABLE: Python package 'cryptography' with Ed25519 support is required; verification did not run.",
        file=sys.stderr,
    )
    raise SystemExit(3)


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


def verify_artifact(arguments: argparse.Namespace) -> dict[str, object]:
    trusted_fingerprint = normalize_fingerprint(arguments.trusted_public_key_fingerprint)
    try:
        public_key_bytes = arguments.public_key.read_bytes()
        manifest_bytes = arguments.manifest.read_bytes()
        signature_text = arguments.signature.read_text(encoding="ascii").strip()
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

    records = manifest.get("public_artifacts")
    if not isinstance(records, list):
        raise VerificationError("signed release manifest has no public artifact records")
    expected_suffix = f".{arguments.artifact_kind}"
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
        raise VerificationError(f"signed release manifest must contain exactly one {arguments.artifact_kind.upper()} artifact")
    record = candidates[0]
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
'@

function Get-VerificationPython {
    $candidates = @(
        [pscustomobject]@{ Command = "py.exe"; Prefix = @("-3") },
        [pscustomobject]@{ Command = "python.exe"; Prefix = @() },
        [pscustomobject]@{ Command = "python3.exe"; Prefix = @() }
    )
    $failures = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in $candidates) {
        $resolved = Get-Command -Name $candidate.Command -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -eq $resolved) {
            continue
        }
        $probeArguments = @($candidate.Prefix) + @("-c", "import cryptography; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey")
        $probeOutput = @(& $resolved.Source $probeArguments 2>&1)
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Command = $resolved.Source; Prefix = @($candidate.Prefix) }
        }
        $failures.Add("$($resolved.Source): $($probeOutput -join ' ')")
    }
    $detail = if ($failures.Count) { " " + ($failures -join " | ") } else { " No Python 3 command was found." }
    throw "Ed25519 verification requires Python 3 with the cryptography package; verification did not run.$detail"
}

function Copy-VerificationMetadata {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if ([string]::IsNullOrWhiteSpace($MetadataDirectory)) {
        $metadataUrl = $MetadataBaseUrl.TrimEnd('/') + "/" + $Name
        Invoke-WebRequest -UseBasicParsing -Uri $metadataUrl -Headers @{ "Cache-Control" = "no-cache" } -OutFile $Destination
        return
    }
    $sourceDirectory = [System.IO.Path]::GetFullPath($MetadataDirectory)
    $sourcePath = Join-Path $sourceDirectory $Name
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Verification metadata file not found: $sourcePath"
    }
    [System.IO.File]::Copy($sourcePath, $Destination, $true)
}

if ([string]::IsNullOrWhiteSpace($FilePath)) {
    $FilePath = Join-Path $env:USERPROFILE ("Downloads\" + $defaultArtifactName)
}
$resolvedArtifactPath = [System.IO.Path]::GetFullPath($FilePath)
if (-not (Test-Path -LiteralPath $resolvedArtifactPath -PathType Leaf)) {
    throw "Downloaded artifact not found: $resolvedArtifactPath"
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("codex-home-manager-verifier-" + [Guid]::NewGuid().ToString("N"))
[System.IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
try {
    $manifestPath = Join-Path $temporaryRoot "release-manifest.json"
    $signaturePath = Join-Path $temporaryRoot "release-manifest.json.sig"
    $publicKeyPath = Join-Path $temporaryRoot "release-signing-public-key.pem"
    Copy-VerificationMetadata -Name "release-manifest.json" -Destination $manifestPath
    Copy-VerificationMetadata -Name "release-manifest.json.sig" -Destination $signaturePath
    Copy-VerificationMetadata -Name "release-signing-public-key.pem" -Destination $publicKeyPath

    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    $pythonVerifierPath = Join-Path $temporaryRoot "verify_release_artifact.py"
    [System.IO.File]::WriteAllText($pythonVerifierPath, $pythonVerifierSource, $utf8WithoutBom)
    $python = Get-VerificationPython
    $verificationArguments = @($python.Prefix) + @(
        $pythonVerifierPath,
        "--manifest", $manifestPath,
        "--signature", $signaturePath,
        "--public-key", $publicKeyPath,
        "--trusted-public-key-fingerprint", $trustedPublicKeyFingerprint,
        "--artifact-kind", $ArtifactKind,
        "--artifact", $resolvedArtifactPath
    )
    $verificationOutput = @(& $python.Command $verificationArguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Release verification failed. $($verificationOutput -join ' ')"
    }
    $result = ($verificationOutput -join "`n") | ConvertFrom-Json
    Write-Host "OK: Ed25519 release signature, pinned SPKI fingerprint, artifact size, and SHA-256 all match."
    Write-Host "Artifact: $($result.manifest_name)"
    Write-Host "SHA-256: $($result.sha256)"
    Write-Host "Pinned Ed25519 SPKI fingerprint: $($result.public_key_fingerprint)"
}
finally {
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force
    }
}
