$ErrorActionPreference = "Stop"

$filePath = Join-Path $env:USERPROFILE "Downloads\codex-home-manager-local-win-x64.exe"
$expectedSha256 = "3508f7d118e3813161cfc195caaa1c3e7161e0a47d0e4acd0da2043da6bd7916"

if (-not (Test-Path -LiteralPath $filePath)) {
    Write-Host "File not found: $filePath"
    Write-Host "Move this script next to the downloaded EXE or edit $filePath, then run it again."
    exit 2
}

$actualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $filePath).Hash.ToLowerInvariant()
if ($actualSha256 -eq $expectedSha256) {
    Write-Host "OK: checksum matches the GitHub Release."
    exit 0
}

Write-Host "FAILED: checksum mismatch. Do not run this file."
Write-Host "Expected: $expectedSha256"
Write-Host "Actual:   $actualSha256"
exit 1