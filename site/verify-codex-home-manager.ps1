$ErrorActionPreference = "Stop"

$filePath = Join-Path $env:USERPROFILE "Downloads\codex-home-manager-local-win-x64-v1.0.2-d267c3929863.exe"
$expectedSha256 = "d267c392986358e87b47b0bb6c85d1ae4095c5a64212d620514736d4d07dcb4c"
$trustedPublicKeyFingerprint = "sha256:ef7194fbc8fa8550430c908d9d02c74f7fc0d1e87f7f9b4ec5a164526b48f208"

if (-not (Test-Path -LiteralPath $filePath)) {
    Write-Host "File not found: $filePath"
    Write-Host "Move this script next to the downloaded EXE or edit $filePath, then run it again."
    exit 2
}

$actualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $filePath).Hash.ToLowerInvariant()
if ($actualSha256 -eq $expectedSha256) {
    Write-Host "OK: checksum matches the release artifact."
    Write-Host "Pinned Ed25519 public key fingerprint: $trustedPublicKeyFingerprint"
    exit 0
}

Write-Host "FAILED: checksum mismatch. Do not run this file."
Write-Host "Expected: $expectedSha256"
Write-Host "Actual:   $actualSha256"
exit 1