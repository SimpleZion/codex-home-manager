from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_process_tree_stop_is_idempotent_when_pid_already_exited() -> None:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script_path}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Stop-ProcessTreeIdempotent' }}, $true) | Select-Object -First 1
Invoke-Expression $function.Extent.Text
$result = Stop-ProcessTreeIdempotent -TargetProcessId 2147483647
[pscustomobject]@{{ result = $result }} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(completed.stdout.strip()) == {"result": "already_exited"}


def test_process_tree_stop_terminates_its_own_test_process_without_a_window() -> None:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script_path}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Stop-ProcessTreeIdempotent' }}, $true) | Select-Object -First 1
Invoke-Expression $function.Extent.Text
$testProcess = Start-Process -FilePath 'pwsh' -ArgumentList @('-NoProfile', '-Command', 'Start-Sleep -Seconds 60') -WindowStyle Hidden -PassThru
try {{
    $result = Stop-ProcessTreeIdempotent -TargetProcessId $testProcess.Id
    $testProcess.WaitForExit(5000) | Out-Null
    $stillRunning = $null -ne (Get-Process -Id $testProcess.Id -ErrorAction SilentlyContinue)
    [pscustomobject]@{{ result = $result; still_running = $stillRunning }} | ConvertTo-Json -Compress
}}
finally {{
    Stop-Process -Id $testProcess.Id -Force -ErrorAction SilentlyContinue
    $testProcess.Dispose()
}}
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(completed.stdout.strip()) == {"result": "stopped", "still_running": False}


def test_process_tree_helper_returns_hash_set_without_pipeline_unrolling() -> None:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{script_path}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-ProcessTreeIds' }}, $true) | Select-Object -First 1
Invoke-Expression $function.Extent.Text
$snapshot = @(
    [pscustomobject]@{{ ProcessId = 101; ParentProcessId = 100 }},
    [pscustomobject]@{{ ProcessId = 102; ParentProcessId = 101 }}
)
$result = Get-ProcessTreeIds -RootProcessId 100 -ProcessSnapshot $snapshot
[pscustomobject]@{{
    type = $result.GetType().FullName
    count = $result.Count
    contains_root = $result.Contains(100)
    contains_descendant = $result.Contains(102)
}} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout.strip())

    assert result["type"].startswith("System.Collections.Generic.HashSet`1[[System.Int32,")
    assert result["count"] == 3
    assert result["contains_root"] is True
    assert result["contains_descendant"] is True


def test_powershell_backup_boundary_rejects_intermediate_junction_escape(tmp_path: Path) -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    required_root = tmp_path / "required-root"
    outside_root = tmp_path / "outside-root"
    required_root.mkdir()
    outside_root.mkdir()
    link_path = required_root / "link"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link_path), str(outside_root)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{runner}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Resolve-SafeContainedPath' }}, $true) | Select-Object -First 1
Invoke-Expression $function.Extent.Text
$rejected = $false
try {{ Resolve-SafeContainedPath -Path '{link_path / "run"}' -RequiredRoot '{required_root}' | Out-Null }} catch {{ $rejected = $true }}
[pscustomobject]@{{ rejected = $rejected }} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(result.stdout.strip()) == {"rejected": True}


def test_installed_plugin_helper_returns_case_insensitive_hash_set(tmp_path: Path) -> None:
    repair_script = Path(__file__).resolve().parents[2] / "scripts" / "repair_codex_bundled_plugins.ps1"
    fake_codex = tmp_path / "fake-codex.cmd"
    fake_codex.write_text(
        '@echo off\necho {"installed":[{"installed":true,"enabled":true,"pluginId":"browser@openai-bundled"}]}\nexit /b 0\n',
        encoding="utf-8",
    )
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{repair_script}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($name in @('Get-BundledPluginRegistry', 'Get-InstalledBundledPluginIds')) {{
    $function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }}, $true) | Select-Object -First 1
    Invoke-Expression $function.Extent.Text
}}
$codexExe = '{fake_codex}'
$result = Get-InstalledBundledPluginIds
[pscustomobject]@{{
    type = $result.GetType().FullName
    count = $result.Count
    contains_different_case = $result.Contains('BROWSER@OPENAI-BUNDLED')
}} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout.strip())

    assert result["type"].startswith("System.Collections.Generic.HashSet`1[[System.String,")
    assert result["count"] == 1
    assert result["contains_different_case"] is True


def test_windows_incompatible_apple_plugins_are_removed_through_official_registry(tmp_path: Path) -> None:
    repair_script = Path(__file__).resolve().parents[2] / "scripts" / "repair_codex_bundled_plugins.ps1"
    removed_path = tmp_path / "removed.txt"
    fake_codex = tmp_path / "fake-codex.ps1"
    fake_codex.write_text(
        rf"""
$removedPath = '{removed_path.as_posix()}'
$pluginIds = @('build-ios-apps@openai-curated', 'build-macos-apps@openai-curated')
$removed = if (Test-Path -LiteralPath $removedPath) {{ @(Get-Content -LiteralPath $removedPath) }} else {{ @() }}
if ($args[0] -eq 'plugin' -and $args[1] -eq 'list') {{
    $installed = @()
    $available = @()
    foreach ($pluginId in $pluginIds) {{
        if ($removed -contains $pluginId) {{
            $available += [pscustomobject]@{{ pluginId = $pluginId; installed = $false; enabled = $false }}
        }}
        else {{
            $installed += [pscustomobject]@{{ pluginId = $pluginId; installed = $true; enabled = $true }}
        }}
    }}
    [pscustomobject]@{{ installed = $installed; available = $available }} | ConvertTo-Json -Compress
    exit 0
}}
if ($args[0] -eq 'plugin' -and $args[1] -eq 'remove') {{
    Add-Content -LiteralPath $removedPath -Value $args[2] -Encoding UTF8
    '{{"removed":true}}'
    exit 0
}}
exit 2
""".lstrip(),
        encoding="utf-8",
    )
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{repair_script}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($name in @('Invoke-CodexCommand', 'Get-CuratedPluginRegistry', 'Assert-NoWindowsIncompatibleRemoteInstallMarkers', 'Ensure-WindowsIncompatiblePluginsRemoved')) {{
    $function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }}, $true) | Select-Object -First 1
    Invoke-Expression $function.Extent.Text
}}
function Assert-CodexOffline {{}}
$codexExe = '{fake_codex.as_posix()}'
Ensure-WindowsIncompatiblePluginsRemoved -CodexHomePath '{tmp_path.as_posix()}' -PluginSelectors @('build-ios-apps@openai-curated', 'build-macos-apps@openai-curated') | Out-Null
[pscustomobject]@{{ removed = @((Get-Content -LiteralPath '{removed_path.as_posix()}') | Sort-Object) }} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])

    assert result["removed"] == ["build-ios-apps@openai-curated", "build-macos-apps@openai-curated"]

    marker_path = (
        tmp_path
        / "plugins"
        / "cache"
        / "openai-curated-remote"
        / "build-ios-apps"
        / ".codex-remote-plugin-install.json"
    )
    marker_path.parent.mkdir(parents=True)
    marker_path.write_text('{"pluginId":"plugins~Plugin_test"}\n', encoding="utf-8")
    blocked = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert blocked.returncode != 0
    assert "Account-synced remote plugin marker remains" in (blocked.stdout + blocked.stderr)


def test_plugin_cache_helper_checks_every_source_file(tmp_path: Path) -> None:
    repair_script = Path(__file__).resolve().parents[2] / "scripts" / "repair_codex_bundled_plugins.ps1"
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    (source / ".codex-plugin").mkdir(parents=True)
    (destination / ".codex-plugin").mkdir(parents=True)
    (source / ".codex-plugin" / "plugin.json").write_text('{"version":"1"}', encoding="utf-8")
    (destination / ".codex-plugin" / "plugin.json").write_text('{"version":"1"}', encoding="utf-8")
    (source / "scripts").mkdir()
    (source / "scripts" / "runtime.mjs").write_text("export const ok = true;\n", encoding="utf-8")
    (destination / "stale.txt").write_text("must be archived", encoding="utf-8")
    backup_root = tmp_path / "backup"

    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{repair_script}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($name in @('Get-PluginTreeManifest', 'Compare-PluginTreeManifest', 'Invoke-RobocopyMirrorWithoutDelete', 'Ensure-PluginCacheComplete')) {{
    $function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }}, $true) | Select-Object -First 1
    Invoke-Expression $function.Extent.Text
}}
function Assert-CodexOffline {{}}
$BackupRoot = '{backup_root}'
Ensure-PluginCacheComplete -Source '{source}' -Destination '{destination}' -RequiredRelativePaths @('.codex-plugin\plugin.json') | Out-Null
[pscustomobject]@{{
    runtime_exists = Test-Path -LiteralPath '{destination / "scripts" / "runtime.mjs"}'
    runtime_hash = if (Test-Path -LiteralPath '{destination / "scripts" / "runtime.mjs"}') {{ (Get-FileHash -Algorithm SHA256 -LiteralPath '{destination / "scripts" / "runtime.mjs"}').Hash }} else {{ $null }}
    source_hash = (Get-FileHash -Algorithm SHA256 -LiteralPath '{source / "scripts" / "runtime.mjs"}').Hash
    stale_exists = Test-Path -LiteralPath '{destination / "stale.txt"}'
    archived_stale_count = @(Get-ChildItem -LiteralPath '{backup_root}' -Recurse -Filter stale.txt -File).Count
}} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout.strip())

    assert result["runtime_exists"] is True
    assert result["runtime_hash"] == result["source_hash"]
    assert result["stale_exists"] is False
    assert result["archived_stale_count"] == 1


def test_plugin_cache_helper_detects_empty_directory_and_entry_type_drift(tmp_path: Path) -> None:
    repair_script = Path(__file__).resolve().parents[2] / "scripts" / "repair_codex_bundled_plugins.ps1"
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    backup_root = tmp_path / "backup"
    (source / ".codex-plugin").mkdir(parents=True)
    (destination / ".codex-plugin").mkdir(parents=True)
    (source / ".codex-plugin" / "plugin.json").write_text('{"version":"1"}', encoding="utf-8")
    (destination / ".codex-plugin" / "plugin.json").write_text('{"version":"1"}', encoding="utf-8")
    (source / "required-empty").mkdir()
    (destination / "extra-empty").mkdir()

    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{repair_script}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($name in @('Get-PluginTreeManifest', 'Compare-PluginTreeManifest', 'Invoke-RobocopyMirrorWithoutDelete', 'Ensure-PluginCacheComplete')) {{
    $function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }}, $true) | Select-Object -First 1
    Invoke-Expression $function.Extent.Text
}}
function Assert-CodexOffline {{}}
$BackupRoot = '{backup_root}'
Ensure-PluginCacheComplete -Source '{source}' -Destination '{destination}' -RequiredRelativePaths @('.codex-plugin\plugin.json') | Out-Null
[pscustomobject]@{{
    required_empty_exists = Test-Path -LiteralPath '{destination / "required-empty"}' -PathType Container
    extra_empty_exists = Test-Path -LiteralPath '{destination / "extra-empty"}'
}} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout.strip())

    assert result == {"required_empty_exists": True, "extra_empty_exists": False}


def test_controlled_cycle_stops_and_restores_local_connector() -> None:
    cycle = Path(__file__).resolve().parents[2] / "scripts" / "run_codex_offline_repair_cycle.ps1"
    source = cycle.read_text(encoding="utf-8")

    assert '$name -eq "codex-home-manager-local-win-x64.exe"' in source
    assert "Start-CodexHomeManagerConnector" in source
    assert 'WindowStyle = "Hidden"' in source
    assert "Get-NetTCPConnection -LocalPort 8765 -State Listen" in source
    assert '"-TimeoutMinutes", "3"' in source
    assert '"-QuietSeconds", "10"' in source


def test_controlled_cycle_can_resume_after_prior_validation_was_superseded() -> None:
    cycle = Path(__file__).resolve().parents[2] / "scripts" / "run_codex_offline_repair_cycle.ps1"
    source = cycle.read_text(encoding="utf-8")

    assert "SkipPriorValidationSupersession" in source
    assert "repair_manifest_chain.py" in source
    assert 'status -ne "validation_superseded"' in source
    assert "prior_validation_already_superseded" in source
    assert "the new repair will create a fresh full prompt audit" in source


def test_controlled_cycle_forwards_targeted_prompt_preserving_slim_threads() -> None:
    cycle = Path(__file__).resolve().parents[2] / "scripts" / "run_codex_offline_repair_cycle.ps1"
    source = cycle.read_text(encoding="utf-8")

    assert "[string[]]$SlimThreadId = @()" in source
    assert 'ConvertTo-Json -InputObject @($normalizedSlimThreadIds) -Compress' in source
    assert '$repairArguments += @("-SlimThreadIdsJson", $slimThreadIdsJson)' in source
    assert "Targeted prompt-preserving slim threads" in source


def test_apple_remote_marker_only_mode_is_read_only_and_runs_while_codex_is_online(tmp_path: Path) -> None:
    repair_script = Path(__file__).resolve().parents[2] / "scripts" / "repair_codex_bundled_plugins.ps1"
    codex_home = tmp_path / "codex_home"
    codex_home.mkdir()
    command = [
        "pwsh",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(repair_script),
        "-CodexHome",
        str(codex_home),
        "-CheckAppleRemoteMarkersOnly",
    ]

    clear = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)
    assert clear.returncode == 0
    assert "No account-synced build-ios/build-macos remote plugin markers" in clear.stdout
    assert list(codex_home.iterdir()) == []

    marker = (
        codex_home
        / "plugins"
        / "cache"
        / "openai-curated-remote"
        / "build-macos-apps"
        / ".codex-remote-plugin-install.json"
    )
    marker.parent.mkdir(parents=True)
    marker.write_text('{"pluginId":"remote"}\n', encoding="utf-8")
    blocked = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=False)

    assert blocked.returncode != 0
    output = blocked.stdout + blocked.stderr
    assert "build-macos-apps" in output
    assert ".codex-remote-plugin-install.json" in output
    assert "Uninstall it from the signed-in Codex Plugins UI" in repair_script.read_text(encoding="utf-8")


def test_cycle_requires_fresh_plan_bound_dpapi_authorization_before_stop() -> None:
    cycle = Path(__file__).resolve().parents[2] / "scripts" / "run_codex_offline_repair_cycle.ps1"
    source = cycle.read_text(encoding="utf-8")
    main_source = source[source.index('$mutexName = "Global\\OpenAI-Codex-Controlled-Offline-Repair-Cycle"') :]
    preflight_source = source[
        source.index("function Invoke-CyclePreflight") : source.index("function Assert-RepairRestartGate")
    ]

    assert '[string]$ShutdownAuthorizationPath = ""' in source
    assert "$effectivePreflightOnly = $PreflightOnly -or -not $authorizationProvided" in main_source
    assert '"authorization_required"' in main_source
    assert "ProtectedData]::Unprotect" in source
    assert "plan_sha256" in source
    assert "plan_nonce" in source
    assert "authorized_at_utc" in source
    assert "AddMinutes(2)" in source
    assert "Start-Sleep -Seconds $ArmSeconds" not in source
    assert preflight_source.index("-PreflightSourceInputsOnly") < preflight_source.index(
        "Invoke-AppleRemoteMarkerPreflight"
    )

    source_preflight = main_source.index("Invoke-CyclePreflight")
    preflight_branch = main_source.index("if ($effectivePreflightOnly)")
    fresh_plan = main_source.index("$shutdownPlan = New-ShutdownPlan", preflight_branch)
    preflight_exit = main_source.index("exit 0", fresh_plan)
    offline_fallback_guard = main_source.index("if (-not $AllowOfflineFallback)", preflight_exit)
    authorization = main_source.index("$authorization = Read-ShutdownAuthorization")
    final_marker_check = main_source.index("Invoke-AppleRemoteMarkerPreflight", authorization)
    consume = main_source.index("Use-ShutdownAuthorization", final_marker_check)
    shutdown_attempt = main_source.index("$shutdownAttempted = $true", consume)
    stop = main_source.index("Stop-CodexRuntime", shutdown_attempt)
    assert (
        source_preflight
        < preflight_branch
        < fresh_plan
        < preflight_exit
        < offline_fallback_guard
        < authorization
        < final_marker_check
        < consume
        < shutdown_attempt
        < stop
    )


def test_controlled_cycle_disables_offline_fallback_by_default() -> None:
    cycle = Path(__file__).resolve().parents[2] / "scripts" / "run_codex_offline_repair_cycle.ps1"
    source = cycle.read_text(encoding="utf-8")

    assert "[switch]$AllowOfflineFallback" in source
    guard_index = source.index("if (-not $AllowOfflineFallback)")
    stop_index = source.index("Stop-CodexRuntime", guard_index)
    guarded_source = source[guard_index:stop_index]
    assert 'Status "online_repair_required"' in guarded_source
    assert "Codex was not stopped" in guarded_source


def test_cycle_preflight_function_declares_the_repair_script_parameter() -> None:
    cycle = Path(__file__).resolve().parents[2] / "scripts" / "run_codex_offline_repair_cycle.ps1"
    command = rf'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{cycle}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-CyclePreflight' }}, $true) | Select-Object -First 1
$parameterNames = @($function.Body.ParamBlock.Parameters | ForEach-Object {{ $_.Name.VariablePath.UserPath }})
[pscustomobject]@{{ parameters = $parameterNames }} | ConvertTo-Json -Compress
'''
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert "RepairScript" in json.loads(completed.stdout)["parameters"]


def test_dpapi_shutdown_authorization_rejects_forgery_expiry_and_replay(tmp_path: Path) -> None:
    cycle = Path(__file__).resolve().parents[2] / "scripts" / "run_codex_offline_repair_cycle.ps1"
    cycle_root = tmp_path / "cycle"
    cycle_root.mkdir()
    command = rf'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{cycle}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($name in @('Resolve-ContainedPath', 'Get-FileSha256', 'Read-ShutdownAuthorization', 'Use-ShutdownAuthorization')) {{
    $function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }}, $true) | Select-Object -First 1
    Invoke-Expression $function.Extent.Text
}}
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$now = [DateTimeOffset]::UtcNow
$plan = [pscustomobject]@{{
    plan_sha256 = ('a' * 64)
    payload = [pscustomobject]@{{
        plan_nonce = ('b' * 64)
        created_at_utc = $now.AddSeconds(-20).UtcDateTime.ToString('o')
        expires_at_utc = $now.AddMinutes(4).UtcDateTime.ToString('o')
    }}
}}
function New-TestAuthorization {{
    param([string]$Path, [string]$PlanHash, [datetimeoffset]$AuthorizedAt, [datetimeoffset]$ExpiresAt)
    $payload = [ordered]@{{
        schema_version = 1
        purpose = 'codex_offline_repair_shutdown'
        approved = $true
        authorization_id = [guid]::NewGuid().ToString('D')
        authorized_at_utc = $AuthorizedAt.UtcDateTime.ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
        expires_at_utc = $ExpiresAt.UtcDateTime.ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
        plan_sha256 = $PlanHash
        plan_nonce = $plan.payload.plan_nonce
    }}
    $payloadBytes = [Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Compress))
    $protectedBytes = [Security.Cryptography.ProtectedData]::Protect(
        $payloadBytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser
    )
    $envelope = [ordered]@{{
        schema_version = 1
        protection = 'windows-dpapi-current-user'
        protected_payload_base64 = [Convert]::ToBase64String($protectedBytes)
    }}
    [IO.File]::WriteAllText($Path, ($envelope | ConvertTo-Json), $utf8NoBom)
}}
$validPath = Join-Path '{cycle_root}' 'valid.json'
New-TestAuthorization -Path $validPath -PlanHash $plan.plan_sha256 -AuthorizedAt $now.AddSeconds(-5) -ExpiresAt $now.AddSeconds(55)
$valid = Read-ShutdownAuthorization -Path $validPath -Plan $plan -CycleRoot '{cycle_root}' -NowUtc $now
$firstUse = Use-ShutdownAuthorization -Authorization $valid -CycleRoot '{cycle_root}'
$replayRejected = $false
try {{ Use-ShutdownAuthorization -Authorization $valid -CycleRoot '{cycle_root}' | Out-Null }} catch {{ $replayRejected = $true }}

$wrongPlanPath = Join-Path '{cycle_root}' 'wrong-plan.json'
New-TestAuthorization -Path $wrongPlanPath -PlanHash ('c' * 64) -AuthorizedAt $now.AddSeconds(-5) -ExpiresAt $now.AddSeconds(55)
$wrongPlanRejected = $false
try {{ Read-ShutdownAuthorization -Path $wrongPlanPath -Plan $plan -CycleRoot '{cycle_root}' -NowUtc $now | Out-Null }} catch {{ $wrongPlanRejected = $true }}

$expiredPath = Join-Path '{cycle_root}' 'expired.json'
New-TestAuthorization -Path $expiredPath -PlanHash $plan.plan_sha256 -AuthorizedAt $now.AddMinutes(-3) -ExpiresAt $now.AddMinutes(-1)
$expiredRejected = $false
try {{ Read-ShutdownAuthorization -Path $expiredPath -Plan $plan -CycleRoot '{cycle_root}' -NowUtc $now | Out-Null }} catch {{ $expiredRejected = $true }}

$tamperedPath = Join-Path '{cycle_root}' 'tampered.json'
Copy-Item -LiteralPath $validPath -Destination $tamperedPath
$tamperedEnvelope = Get-Content -LiteralPath $tamperedPath -Raw | ConvertFrom-Json
$tamperedBytes = [Convert]::FromBase64String([string]$tamperedEnvelope.protected_payload_base64)
$tamperedBytes[0] = $tamperedBytes[0] -bxor 1
$tamperedEnvelope.protected_payload_base64 = [Convert]::ToBase64String($tamperedBytes)
[IO.File]::WriteAllText($tamperedPath, ($tamperedEnvelope | ConvertTo-Json), $utf8NoBom)
$tamperRejected = $false
try {{ Read-ShutdownAuthorization -Path $tamperedPath -Plan $plan -CycleRoot '{cycle_root}' -NowUtc $now | Out-Null }} catch {{ $tamperRejected = $true }}

[pscustomobject]@{{
    valid_id = $valid.authorization_id
    consumed_exists = Test-Path -LiteralPath $firstUse
    replay_rejected = $replayRejected
    wrong_plan_rejected = $wrongPlanRejected
    expired_rejected = $expiredRejected
    tamper_rejected = $tamperRejected
}} | ConvertTo-Json -Compress
'''
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout.strip())

    assert result["valid_id"]
    assert result["consumed_exists"] is True
    assert result["replay_rejected"] is True
    assert result["wrong_plan_rejected"] is True
    assert result["expired_rejected"] is True
    assert result["tamper_rejected"] is True


def test_shutdown_plan_is_random_short_lived_and_bound_to_current_inputs(tmp_path: Path) -> None:
    cycle = Path(__file__).resolve().parents[2] / "scripts" / "run_codex_offline_repair_cycle.ps1"
    plan_path = tmp_path / "shutdown_plan.json"
    command = rf'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{cycle}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($name in @('Get-JsonSha256', 'Write-JsonAtomic', 'New-ShutdownPlan', 'Read-ShutdownPlan')) {{
    $function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }}, $true) | Select-Object -First 1
    Invoke-Expression $function.Extent.Text
}}
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$now = [DateTimeOffset]::UtcNow
$binding = [ordered]@{{ pending_manifest_sha256 = ('a' * 64); slim_thread_ids = @('thread-a') }}
$first = New-ShutdownPlan -Path '{plan_path}' -Binding $binding -NowUtc $now
$validated = Read-ShutdownPlan -Path '{plan_path}' -ExpectedBinding $binding -NowUtc $now.AddSeconds(1)
$wrongBindingRejected = $false
try {{
    Read-ShutdownPlan -Path '{plan_path}' -ExpectedBinding ([ordered]@{{ pending_manifest_sha256 = ('b' * 64); slim_thread_ids = @('thread-a') }}) -NowUtc $now.AddSeconds(1) | Out-Null
}}
catch {{ $wrongBindingRejected = $true }}
$expiredRejected = $false
try {{ Read-ShutdownPlan -Path '{plan_path}' -ExpectedBinding $binding -NowUtc $now.AddMinutes(6) | Out-Null }} catch {{ $expiredRejected = $true }}
$second = New-ShutdownPlan -Path '{plan_path}' -Binding $binding -NowUtc $now.AddSeconds(2)
[pscustomobject]@{{
    validated_hash = $validated.plan_sha256
    hash_matches = $validated.plan_sha256 -eq $first.plan_sha256
    nonce_length = ([string]$first.payload.plan_nonce).Length
    nonce_rotated = $first.payload.plan_nonce -ne $second.payload.plan_nonce
    wrong_binding_rejected = $wrongBindingRejected
    expired_rejected = $expiredRejected
}} | ConvertTo-Json -Compress
'''
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout.strip())

    assert len(result["validated_hash"]) == 64
    assert result["hash_matches"] is True
    assert result["nonce_length"] == 64
    assert result["nonce_rotated"] is True
    assert result["wrong_binding_rejected"] is True
    assert result["expired_rejected"] is True


def test_stale_restore_archive_happens_after_snapshot_and_recovery_is_repeatable(
    tmp_path: Path,
) -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    source = runner.read_text(encoding="utf-8")
    assert source.index('-Name "00_plugin_snapshot"') < source.index(
        '-Name "00_archive_stale_restores"'
    )

    codex_home = tmp_path / "codex-home"
    archive_root = tmp_path / "backup" / "stale_restore_artifacts"
    codex_home.mkdir()
    archive_root.mkdir(parents=True)
    first_name = f".plugins.{'a' * 32}.restoring"
    second_name = f".plugins.{'b' * 32}.restoring"
    (archive_root / first_name).mkdir()
    (archive_root / first_name / "first.txt").write_text("first", encoding="utf-8")
    (archive_root / second_name).mkdir()
    (archive_root / second_name / "second.txt").write_text("second", encoding="utf-8")
    (codex_home / second_name).mkdir()
    collision_saved = codex_home / "collision.saved"
    command = rf'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{runner}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Restore-StaleRestoreArtifacts' }}, $true) | Select-Object -First 1
Invoke-Expression $function.Extent.Text
function Get-CodexProcesses {{ return @() }}
$firstFailed = $false
try {{
    Restore-StaleRestoreArtifacts -ArchiveRoot '{archive_root}' -CodexHomePath '{codex_home}' | Out-Null
}}
catch {{ $firstFailed = $true }}
Move-Item -LiteralPath '{codex_home / second_name}' -Destination '{collision_saved}'
$secondRestored = Restore-StaleRestoreArtifacts -ArchiveRoot '{archive_root}' -CodexHomePath '{codex_home}'
$thirdRestored = Restore-StaleRestoreArtifacts -ArchiveRoot '{archive_root}' -CodexHomePath '{codex_home}'
[pscustomobject]@{{
    first_failed = $firstFailed
    first_restored = Test-Path -LiteralPath '{codex_home / first_name / "first.txt"}'
    second_restored = Test-Path -LiteralPath '{codex_home / second_name / "second.txt"}'
    second_count = $secondRestored
    third_count = $thirdRestored
    archived_remaining = @(Get-ChildItem -LiteralPath '{archive_root}' -Force | Where-Object {{ $_.Name -match '^\..+\.[0-9a-fA-F]{{32}}\.restoring$' }}).Count
}} | ConvertTo-Json -Compress
'''
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(completed.stdout.strip()) == {
        "first_failed": True,
        "first_restored": True,
        "second_restored": True,
        "second_count": 1,
        "third_count": 0,
        "archived_remaining": 0,
    }


def test_rollback_offline_probe_exceptions_fail_closed_without_escaping() -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    command = rf'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{runner}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-RollbackOfflineState' }}, $true) | Select-Object -First 1
Invoke-Expression $function.Extent.Text
$processProbeCalled = $false
$waitFailure = Get-RollbackOfflineState -WaitProbe {{ throw 'injected wait failure' }} -ProcessProbe {{ $processProbeCalled = $true; @() }}
$cimFailure = Get-RollbackOfflineState -WaitProbe {{ $true }} -ProcessProbe {{ throw 'injected CIM failure' }}
[pscustomobject]@{{
    wait_succeeded = $waitFailure.probe_succeeded
    wait_offline = $waitFailure.offline_confirmed
    wait_error = $waitFailure.error
    process_probe_called = $processProbeCalled
    cim_succeeded = $cimFailure.probe_succeeded
    cim_offline = $cimFailure.offline_confirmed
    cim_error = $cimFailure.error
}} | ConvertTo-Json -Compress
'''
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(completed.stdout.strip()) == {
        "wait_succeeded": False,
        "wait_offline": False,
        "wait_error": "injected wait failure",
        "process_probe_called": False,
        "cim_succeeded": False,
        "cim_offline": False,
        "cim_error": "injected CIM failure",
    }


def test_runner_persists_failure_state_before_unsafe_rollback_probe() -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    source = runner.read_text(encoding="utf-8")
    catch_source = source[source.index("catch {", source.index("$scriptExitCode = 1")) :]

    preserve = catch_source.index("$preserveActiveLock = $true")
    initial_failure = catch_source.index("$initialFailure = [ordered]@{")
    persist = catch_source.index("Save-RepairFailureArtifacts")
    probe = catch_source.index("Get-RollbackOfflineState")
    assert preserve < initial_failure < persist < probe
    assert 'FailureLockStatus "failed_pending_offline_assessment"' in catch_source
    assert '"offline_probe_failed"' in catch_source


def test_failure_manifest_and_active_lock_are_atomically_repeatable(tmp_path: Path) -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    failure_path = tmp_path / "run" / "FAILED.txt"
    lock_path = tmp_path / "active_repair.lock.json"
    lock_path.write_text('{"run_id":"preexisting"}', encoding="utf-8")
    command = rf'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{runner}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($name in @('Write-Utf8FileDurable', 'Write-JsonAtomicDurable', 'Save-RepairFailureArtifacts')) {{
    $function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }}, $true) | Select-Object -First 1
    Invoke-Expression $function.Extent.Text
}}
$initial = [ordered]@{{ status = 'failed'; error = 'injected'; rollback_status = 'rollback_probe_pending'; rollback_errors = @() }}
$firstErrors = @(Save-RepairFailureArtifacts -FailurePayload $initial -FailureManifestPath '{failure_path}' -ActiveLockPath '{lock_path}' -OwnsActiveLock $true -FailureLockStatus 'failed_pending_offline_assessment' -CurrentRunId 'run-001' -CurrentRunRoot '{tmp_path / "run"}' -CurrentMutexName 'test-mutex' -CurrentSourceBinding $null -CurrentSourceBindingSha256 $null)
$final = [ordered]@{{ status = 'failed'; error = 'injected'; rollback_status = 'pending_offline_rollback'; rollback_errors = @('injected CIM failure') }}
$secondErrors = @(Save-RepairFailureArtifacts -FailurePayload $final -FailureManifestPath '{failure_path}' -ActiveLockPath '{lock_path}' -OwnsActiveLock $true -FailureLockStatus 'pending_offline_rollback' -CurrentRunId 'run-001' -CurrentRunRoot '{tmp_path / "run"}' -CurrentMutexName 'test-mutex' -CurrentSourceBinding $null -CurrentSourceBindingSha256 $null)
$failure = Get-Content -LiteralPath '{failure_path}' -Raw | ConvertFrom-Json
$lock = Get-Content -LiteralPath '{lock_path}' -Raw | ConvertFrom-Json
[pscustomobject]@{{
    first_error_count = $firstErrors.Count
    second_error_count = $secondErrors.Count
    failure_status = [string]$failure.rollback_status
    failure_error = [string]$failure.rollback_errors[0]
    lock_status = [string]$lock.status
    lock_run_id = [string]$lock.run_id
    writing_files = @(Get-ChildItem -LiteralPath '{tmp_path}' -Recurse -Force -Filter '*.writing').Count
}} | ConvertTo-Json -Compress
'''
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(completed.stdout.strip()) == {
        "first_error_count": 0,
        "second_error_count": 0,
        "failure_status": "pending_offline_rollback",
        "failure_error": "injected CIM failure",
        "lock_status": "pending_offline_rollback",
        "lock_run_id": "run-001",
        "writing_files": 0,
    }


def test_cycle_restart_gate_rejects_nonpending_manifest_and_lock(tmp_path: Path) -> None:
    cycle = Path(__file__).resolve().parents[2] / "scripts" / "run_codex_offline_repair_cycle.ps1"
    backup_root = tmp_path / "backup"
    run_root = backup_root / "run"
    manifest_path = run_root / "repair_data" / "repair_manifest.json"
    pending_path = run_root / "PENDING_RESTART_VALIDATION.json"
    lock_path = backup_root / "active_repair.lock.json"
    inspector_path = tmp_path / "manifest_inspector.py"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{}", encoding="utf-8")
    manifest_hash = "a" * 64
    pending_path.write_text(
        json.dumps(
            {
                "status": "pending_restart_validation",
                "run_id": "run-001",
                "manifest": str(manifest_path),
            }
        ),
        encoding="utf-8",
    )
    lock = {
        "status": "pending_restart_validation",
        "run_id": "run-001",
        "run_root": str(run_root),
        "manifest": str(manifest_path),
        "pending_validation": str(pending_path),
        "repair_manifest_sha256": manifest_hash,
    }
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    inspector_path.write_text(
        "import json, os\n"
        f"print(json.dumps({{'sha256': '{manifest_hash}', 'payload': {{"
        "'status': os.environ.get('INJECTED_MANIFEST_STATUS', 'pending_restart_validation'), "
        f"'runner_run_id': 'run-001', 'run_root': {str(run_root)!r}}}}}))\n",
        encoding="utf-8",
    )
    command = rf'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{cycle}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($name in @('Resolve-ContainedPath', 'Assert-RepairRestartGate')) {{
    $function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }}, $true) | Select-Object -First 1
    Invoke-Expression $function.Extent.Text
}}
$passed = $true
try {{ Assert-RepairRestartGate -BackupRoot '{backup_root}' -PythonPath '{sys.executable}' -ManifestInspector '{inspector_path}' | Out-Null }} catch {{ $passed = $false }}
$env:INJECTED_MANIFEST_STATUS = 'failed'
$manifestRejected = $false
try {{ Assert-RepairRestartGate -BackupRoot '{backup_root}' -PythonPath '{sys.executable}' -ManifestInspector '{inspector_path}' | Out-Null }} catch {{ $manifestRejected = $true }}
$env:INJECTED_MANIFEST_STATUS = 'pending_restart_validation'
$lock = Get-Content -LiteralPath '{lock_path}' -Raw | ConvertFrom-Json
$lock.status = 'failed_rolled_back'
$lock | ConvertTo-Json | Set-Content -LiteralPath '{lock_path}' -Encoding UTF8
$lockRejected = $false
try {{ Assert-RepairRestartGate -BackupRoot '{backup_root}' -PythonPath '{sys.executable}' -ManifestInspector '{inspector_path}' | Out-Null }} catch {{ $lockRejected = $true }}
[pscustomobject]@{{ passed = $passed; manifest_rejected = $manifestRejected; lock_rejected = $lockRejected }} | ConvertTo-Json -Compress
'''
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(completed.stdout.strip()) == {
        "passed": True,
        "manifest_rejected": True,
        "lock_rejected": True,
    }


def test_cycle_restores_runtime_after_failure_without_claiming_repair_success() -> None:
    cycle = Path(__file__).resolve().parents[2] / "scripts" / "run_codex_offline_repair_cycle.ps1"
    source = cycle.read_text(encoding="utf-8")
    finally_source = source[source.index("finally {", source.index("$restartGate = $null")) :]

    guard = finally_source.index("if ($shutdownAttempted)")
    success_gate = finally_source.index(
        "$repairSuccessGatePassed = $restartAuthorized -and $repairExitCode -eq 0 -and $null -eq $cycleError"
    )
    restore_call = finally_source.index("$restoreResult = Restore-CapturedRuntime")
    failed_restore = finally_source.index('Status "runtime_restored_after_repair_failure"')
    restore_helper = source[
        source.index("function Restore-CapturedRuntime") : source.index("$mutexName", source.index("function Restore-CapturedRuntime"))
    ]
    assert "Start-CodexDesktop" in restore_helper
    assert "Start-CodexHomeManagerConnector" in restore_helper
    assert guard < success_gate < restore_call < failed_restore
    assert 'repair_success_gate_passed = $false' in finally_source
    assert 'Status "runtime_restored_pending_live_validation"' in finally_source
    assert "restart_blocked_fail_closed" not in finally_source


def test_offline_runner_binds_every_json_slim_thread_id_in_a_real_child_process() -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell 7 is unavailable")

    thread_ids = ["thread-a", "thread-b", "thread-c"]
    completed = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(runner),
            "-SlimThreadIdsJson",
            json.dumps(thread_ids, separators=(",", ":")),
            "-PrintNormalizedSlimThreadIdsAndExit",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip()) == thread_ids


def test_offline_runner_logs_blocking_process_identity() -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    source = runner.read_text(encoding="utf-8")

    assert "lastBlockingSignature" in source
    assert '"$($_.Name):$($_.ProcessId):$($_.ParentProcessId)"' in source
    assert "Still waiting for Codex process(es):" in source
    assert "Remaining: $lastBlockingSignature" in source


def test_latest_plugin_path_is_replaced_with_exact_junction_and_archived(tmp_path: Path) -> None:
    repair_script = Path(__file__).resolve().parents[2] / "scripts" / "repair_codex_bundled_plugins.ps1"
    target = tmp_path / "cache" / "1.0.0"
    latest = tmp_path / "cache" / "latest"
    backup_root = tmp_path / "backup"
    (target / ".codex-plugin").mkdir(parents=True)
    (target / ".codex-plugin" / "plugin.json").write_text('{"version":"1.0.0"}', encoding="utf-8")
    (latest / ".codex-plugin").mkdir(parents=True)
    (latest / ".codex-plugin" / "plugin.json").write_text('{"version":"old"}', encoding="utf-8")

    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{repair_script}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Ensure-Junction' }}, $true) | Select-Object -First 1
Invoke-Expression $function.Extent.Text
$BackupRoot = '{backup_root}'
Ensure-Junction -Path '{latest}' -Target '{target}' | Out-Null
$item = Get-Item -LiteralPath '{latest}' -Force
$archives = @(Get-ChildItem -LiteralPath '{backup_root}' -Recurse -Filter plugin.json -File)
[pscustomobject]@{{
    link_type = [string]$item.LinkType
    target = [string](@($item.Target)[0])
    expected_target = [string](Resolve-Path -LiteralPath '{target}')
    archived_manifest_count = $archives.Count
}} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout.strip())

    assert result["link_type"] == "Junction"
    assert Path(result["target"]).resolve() == Path(result["expected_target"]).resolve()
    assert result["archived_manifest_count"] == 1


def test_registry_verifier_checks_version_and_exact_local_sources(tmp_path: Path) -> None:
    repair_script = Path(__file__).resolve().parents[2] / "scripts" / "repair_codex_bundled_plugins.ps1"
    marketplace = tmp_path / "marketplace"
    plugin_source = marketplace / "plugins" / "browser"
    plugin_source.mkdir(parents=True)
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{repair_script}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
$function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-BundledPluginRegistryEntry' }}, $true) | Select-Object -First 1
Invoke-Expression $function.Extent.Text
$entry = [pscustomobject]@{{
    pluginId = 'browser@openai-bundled'
    installed = $true
    enabled = $true
    version = '1.2.3'
    source = [pscustomobject]@{{ source = 'local'; path = '{plugin_source}' }}
    marketplaceSource = [pscustomobject]@{{ sourceType = 'local'; source = '{marketplace}' }}
}}
$registry = [pscustomobject]@{{ installed = @($entry) }}
Assert-BundledPluginRegistryEntry -Registry $registry -PluginName browser -ExpectedVersion '1.2.3' -ExpectedPluginSource '{plugin_source}' -ExpectedMarketplaceSource '{marketplace}'
$wrongVersionRejected = $false
try {{
    Assert-BundledPluginRegistryEntry -Registry $registry -PluginName browser -ExpectedVersion '9.9.9' -ExpectedPluginSource '{plugin_source}' -ExpectedMarketplaceSource '{marketplace}'
}}
catch {{
    $wrongVersionRejected = $true
}}
[pscustomobject]@{{ exact_passed = $true; wrong_version_rejected = $wrongVersionRejected }} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout.strip())

    assert result == {"exact_passed": True, "wrong_version_rejected": True}


def test_bundled_repair_temporarily_registers_then_releases_desktop_marketplace_source() -> None:
    repair_script = Path(__file__).resolve().parents[2] / "scripts" / "repair_codex_bundled_plugins.ps1"
    source = repair_script.read_text(encoding="utf-8")

    assert "$registrationMarketplace = $temporaryMarketplace" in source
    assert source.count("-BundledMarketplacePath $registrationMarketplace") == 1
    assert source.count("-ExpectedMarketplaceSource $registrationMarketplace") >= 2
    assert source.count('Join-Path $registrationMarketplace "plugins\\$pluginName"') >= 3
    assert "finally {" in source
    assert "-ReleaseDesktopMarketplaceOwnership" in source
    assert source.count('@("--remove-marketplace", "openai-primary-runtime")') >= 2


def test_one_shot_runner_has_mutex_snapshot_rollback_and_pending_validation_contract() -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    source = runner.read_text(encoding="utf-8")

    assert "[System.Threading.Mutex]::new" in source
    assert "[guid]::NewGuid()" in source
    assert '"00_archive_stale_restores"' in source
    assert '"archive-stale-restores"' in source
    assert '"00_plugin_snapshot"' in source
    assert '"90_rollback_threads"' in source
    assert '"91_rollback_plugins"' not in source
    assert 'status = "pending_restart_validation"' in source
    assert "ExecutionTimeoutMinutes" in source
    assert "Wait-CodexOfflineForRollback" in source
    assert "ABANDONED_RUN_RECOVERED.json" in source
    assert '$staleManifest.status -notin @("rolled_back", "complete", "validation_superseded")' in source
    assert '$staleManifest.status -in @("complete", "validation_superseded")' in source
    assert '$staleManifest.status -eq "rolled_back"' in source
    assert "expected-audit-sha256" in source
    assert "[string[]]$SlimThreadId = @()" in source
    assert '[string]$SlimThreadIdsJson = ""' in source
    assert '$applyArguments += @("--slim-thread-id", $targetThreadId.Trim())' in source
    assert "plugin-snapshot-manifest" in source
    assert "$preserveActiveLock" in source
    assert "if ($activeLockOwnedByCurrentRun -and -not $preserveActiveLock" in source
    assert "Existing active repair lock could not be safely adopted; it was left unchanged" in source
    assert "Resolve-SafeContainedPath -Path ([string]$staleLock.run_root)" in source
    assert "must keep its active lock" in source
    assert "Codex restarted before the pending-validation manifest swap" in source
    assert 'status = "pending_restart_validation"' in source
    assert "$preserveActiveLock = $true" in source
    assert "$activeLockOwnedByCurrentRun = $false" in source
    assert "The abandoned transaction lock changed during recovery" in source
    assert "Save-RepairFailureArtifacts" in source
    assert '-OwnsActiveLock $activeLockOwnedByCurrentRun' in source
    assert '"--expected-run-id", $staleRunId' in source
    assert '"--expected-manifest-sha256", $staleManifestSha256' in source
    assert '"--preserve-runtime-state"' in source
    assert 'status = "complete"' not in source
    assert "New-RepairSourceBinding" in source
    assert "Assert-RepairSourceBinding" in source
    assert "PreflightSourceInputsOnly" in source
    assert "Assert-RepairSourceInputsReady" in source
    assert "git -C $owningRepository[0] ls-files --error-unmatch" in source
    assert "source_binding_sha256" in source
    assert "[switch]$RecoverOnly" in source
    assert "RecoverOnly requires an existing safely adopted repair transaction lock" in source
    assert "RECOVERY_ONLY_COMPLETE.json" in source
    assert "Recovery-only mode completed. No new repair stages were started." in source
    assert "function Stop-OrphanedCodexPluginExtensionHosts" in source
    assert "function Stop-ProcessTreeIdempotent" in source
    assert "[System.Diagnostics.ProcessStartInfo]::new()" in source
    assert 'return "already_exited"' in source
    assert "function Get-CodexNativeHostBrowserRootProcessIds" in source
    assert "if (@(Get-CodexCoreProcesses).Count -eq 0)" in source
    assert 'Test-CodexPluginExtensionHostProcess -ProcessRecord $_' in source
    assert "[System.Collections.Generic.HashSet[int]]$AllowedCoreProcessIds" in source
    assert "An external Codex core process started during stage" in source
    assert "Stop-OrphanedCodexPluginExtensionHosts -AllowedCoreProcessIds $allowedProcessIds" in source
    assert "A Codex core process is running before stage" in source
    assert "left a Codex core process running" in source
    assert '$name -ieq "codex-home-manager-local-win-x64.exe"' in source
    assert '$name -ieq "chrome.exe"' not in source
    assert '$name -ieq "msedge.exe"' not in source
    assert "exited before taskkill completed" in source
    assert 'Get-CimInstance Win32_Process -Filter "ProcessId = $rootProcessId"' in source
    assert "browser root(s) that launched them" in source
    assert "QuietPeriodMilliseconds = 1500" in source
    assert "TimeoutSeconds = 15" in source


def test_extension_host_cleanup_classifier_is_scoped_to_current_codex_home() -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    source = runner.read_text(encoding="utf-8")
    function_names = [
        "Test-CodexPluginExtensionHostProcess",
        "Test-CodexCoreProcess",
    ]
    functions = []
    for function_name in function_names:
        start = source.index(f"function {function_name}")
        next_function = source.find("\nfunction ", start + 1)
        functions.append(source[start:] if next_function < 0 else source[start:next_function])
    function_source = "\n".join(functions)
    command = f"""
$CodexHome = 'D:\\.codex'
{function_source}
$records = @(
    [pscustomobject]@{{ Name = 'extension-host.exe'; CommandLine = '\"D:\\.codex\\plugins\\cache\\openai-bundled\\chrome\\latest\\extension-host\\windows\\x64\\extension-host.exe\" chrome-extension://id/' }},
    [pscustomobject]@{{ Name = 'cmd.exe'; CommandLine = 'cmd /c \"D:\\.codex\\plugins\\cache\\openai-bundled\\chrome\\latest\\extension-host\\windows\\x64\\extension-host.exe\"' }},
    [pscustomobject]@{{ Name = 'extension-host.exe'; CommandLine = '\"E:\\.codex\\plugins\\cache\\openai-bundled\\chrome\\latest\\extension-host\\windows\\x64\\extension-host.exe\" chrome-extension://id/' }},
    [pscustomobject]@{{ Name = 'chrome.exe'; CommandLine = '\"D:\\.codex\\plugins\\cache\\openai-bundled\\chrome\\latest\\extension-host\\windows\\x64\\extension-host.exe\"' }},
    [pscustomobject]@{{ Name = 'ChatGPT.exe'; CommandLine = '\"C:\\Program Files\\OpenAI\\Codex\\ChatGPT.exe\"' }}
)
[pscustomobject]@{{
    plugin_matches = @($records | ForEach-Object {{ Test-CodexPluginExtensionHostProcess -ProcessRecord $_ }})
    core_matches = @($records | ForEach-Object {{ Test-CodexCoreProcess -ProcessRecord $_ }})
}} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout.strip())

    assert result["plugin_matches"] == [True, True, False, False, False]
    assert result["core_matches"] == [False, False, False, False, True]


def test_extension_host_cleanup_resolves_the_browser_root_that_spawned_native_host() -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    source = runner.read_text(encoding="utf-8")
    function_name = "Get-CodexNativeHostBrowserRootProcessIds"
    start = source.index(f"function {function_name}")
    next_function = source.find("\nfunction ", start + 1)
    function_source = source[start:] if next_function < 0 else source[start:next_function]
    command = f"""
{function_source}
$snapshot = @(
    [pscustomobject]@{{ ProcessId = 10; ParentProcessId = 1; Name = 'chrome.exe' }},
    [pscustomobject]@{{ ProcessId = 20; ParentProcessId = 10; Name = 'cmd.exe' }},
    [pscustomobject]@{{ ProcessId = 30; ParentProcessId = 20; Name = 'extension-host.exe' }},
    [pscustomobject]@{{ ProcessId = 40; ParentProcessId = 1; Name = 'msedge.exe' }}
)
$extensionHosts = @($snapshot | Where-Object {{ $_.ProcessId -in @(20, 30) }})
$roots = Get-CodexNativeHostBrowserRootProcessIds -ExtensionHosts $extensionHosts -ProcessSnapshot $snapshot
[pscustomobject]@{{ count = $roots.Count; contains_chrome = $roots.Contains(10); contains_edge = $roots.Contains(40) }} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    result = json.loads(completed.stdout.strip())

    assert result == {"count": 1, "contains_chrome": True, "contains_edge": False}


def test_plugin_repair_requires_connector_but_not_unrelated_browsers_to_be_offline() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "repair_codex_bundled_plugins.ps1"
    source = script.read_text(encoding="utf-8")

    assert '$name -ieq "codex-home-manager-local-win-x64.exe"' in source
    assert '$name -ieq "chrome.exe"' not in source
    assert '$name -ieq "msedge.exe"' not in source


def test_stage_argument_serializer_preserves_spaces_quotes_and_trailing_slashes(
    tmp_path: Path,
) -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    source = runner.read_text(encoding="utf-8")
    start = source.index("function New-RepairStageStartInfo")
    next_function = source.find("\nfunction ", start + 1)
    function_source = source[start:] if next_function < 0 else source[start:next_function]
    output_path = tmp_path / "captured arguments.json"
    expected_arguments = [
        r"C:\Program Files\WindowsApps\OpenAI.Codex\plugins\openai-bundled",
        'value with "embedded quotes"',
        "",
        "plain-value",
        "C:\\path with space\\",
    ]
    arguments_json = json.dumps(expected_arguments, ensure_ascii=True)
    command = f"""
{function_source}
$arguments = ConvertFrom-Json @'
{arguments_json}
'@
$pythonCode = 'import json, pathlib, sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding="utf-8")'
$rawArguments = @('-c', $pythonCode, '{output_path}') + @($arguments)
$startInfo = New-RepairStageStartInfo -FilePath 'python' -ArgumentList $rawArguments -WorkingDirectory '{tmp_path}'
$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $startInfo
$null = $process.Start()
$stdout = $process.StandardOutput.ReadToEnd()
$stderr = $process.StandardError.ReadToEnd()
$process.WaitForExit()
if ($process.ExitCode -ne 0) {{ throw "Child process exited with $($process.ExitCode)" }}
$process.Dispose()
"""
    subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(output_path.read_text(encoding="utf-8")) == expected_arguments


def test_one_shot_runner_uses_only_committed_repair_manifest_head() -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    source = runner.read_text(encoding="utf-8")

    assert "function Get-CommittedRepairManifest" in source
    assert source.count("Get-CommittedRepairManifest -ManifestPath") >= 4
    assert "Get-FileHash -Algorithm SHA256 -LiteralPath $staleRepairManifest" not in source
    assert "Get-FileHash -Algorithm SHA256 -LiteralPath $repairManifestPath" not in source
    assert "Get-FileHash -Algorithm SHA256 -LiteralPath $ManifestPath" not in source


def test_one_shot_runner_requires_runtime_marketplace_source_diagnostic() -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    source = runner.read_text(encoding="utf-8")

    assert '"--require-pass", "plugins.bundled_marketplace_source"' in source
    assert '"--require-pass", "plugins.stale_restore_artifacts"' in source


def test_one_shot_runner_preserves_validation_superseded_transaction() -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    source = runner.read_text(encoding="utf-8")

    assert source.count('"validation_superseded"') >= 2
    assert '$staleManifest.status -in @("complete", "validation_superseded")' in source
    assert '$staleManifest.status -notin @("rolled_back", "complete", "validation_superseded")' in source


def test_real_runner_bound_source_inventory_is_fully_git_tracked() -> None:
    manager_root = Path(__file__).resolve().parents[1]
    root_repository = manager_root.parent
    root_scripts = root_repository / "scripts"
    root_script_names = {
        "apply_codex_offline_repair.py",
        "audit_codex_thread_histories.py",
        "codex_plugin_state_snapshot.py",
        "collect_codex_live_validation.py",
        "live_validation_contract.py",
        "merge_codex_managed_config.py",
        "merge_codex_runtime_config.py",
        "repair_all_codex_after_exit.ps1",
        "repair_codex_bundled_plugins.ps1",
        "repair_manifest_chain.py",
        "run_codex_diagnostics_snapshot.py",
        "supersede_pending_repair_validation.py",
        "verify_codex_after_restart.py",
    }
    required_paths = {
        root_repository / "AGENTS.md",
        root_repository / "pytest.ini",
        *(root_scripts / name for name in root_script_names),
        *(path for path in (manager_root / "tests").glob("test_*.py") if path.is_file()),
        *(path for path in (manager_root / "backend").rglob("*.py") if path.is_file()),
    }
    source_manifest_path = root_repository / "SOURCE_COMMITS.json"
    failures: list[str] = []
    if source_manifest_path.is_file():
        source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
        manifest_paths = {
            record["path"]
            for source in source_manifest["sources"].values()
            for record in source["files"]
        }
        for path in sorted(required_paths, key=lambda candidate: str(candidate).casefold()):
            if path.is_file():
                relative_path = path.relative_to(root_repository).as_posix()
                if relative_path not in manifest_paths:
                    failures.append(relative_path)
    else:
        for path in sorted(required_paths, key=lambda candidate: str(candidate).casefold()):
            repository = manager_root if manager_root in path.parents else root_repository
            relative_path = path.relative_to(repository).as_posix()
            completed = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", relative_path],
                cwd=repository,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if completed.returncode != 0:
                failures.append(f"{repository.name}/{relative_path}")

    assert failures == [], "runner-bound sources missing from Git index: " + ", ".join(failures)

    runner_source = (root_scripts / "repair_all_codex_after_exit.ps1").read_text(encoding="utf-8")
    assert 'Get-ChildItem -LiteralPath $PSScriptRoot -Recurse' not in runner_source
    for name in root_script_names - {"repair_all_codex_after_exit.ps1"}:
        assert name in runner_source
    assert "$MyInvocation.MyCommand.Path" in runner_source


def test_plugin_repair_syncs_managed_config_after_final_runtime_config() -> None:
    repair_script = Path(__file__).resolve().parents[2] / "scripts" / "repair_codex_bundled_plugins.ps1"
    source = repair_script.read_text(encoding="utf-8")
    runtime_call = source.rfind("Ensure-RuntimeConfig -CodexHomePath")
    managed_call = source.rfind("Ensure-ManagedBundledPluginConfig -CodexHomePath")

    assert runtime_call > 0
    assert managed_call > runtime_call
    assert '"--runtime-config"' not in source
    finalizer = source.rfind("finally {")
    final_registry_call = source.rfind(
        "Ensure-WindowsIncompatiblePluginsRemoved -CodexHomePath $codexHomePath -PluginSelectors $windowsIncompatiblePluginSelectors"
    )
    assert final_registry_call < finalizer < runtime_call < managed_call
    assert "Invoke-CodexCommand" not in source[finalizer:]
    assert 'Join-Path $CodexHome "plugins\\.plugin-appserver\\codex.exe"' in source


def test_plugin_repair_blocks_account_synced_apple_markers_before_cache_writes() -> None:
    repair_script = Path(__file__).resolve().parents[2] / "scripts" / "repair_codex_bundled_plugins.ps1"
    source = repair_script.read_text(encoding="utf-8")

    preflight_call = source.index(
        "Assert-NoWindowsIncompatibleRemoteInstallMarkers -CodexHomePath $codexHomePath"
    )
    first_cache_write = source.index("Ensure-PluginCacheComplete -Source")
    assert preflight_call < first_cache_write
    assert '"--codex-cli-path"' not in source
    assert "-CodexCliPath" not in source
    assert ".codex-remote-plugin-install.json" in source


def test_runner_verifies_bound_appx_tree_before_and_after_plugin_repair() -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    source = runner.read_text(encoding="utf-8")

    marker = '"verify-sources", "--manifest", $pluginSnapshotManifest'
    assert source.count(marker) == 2
    before_repair = source.index(marker)
    repair_stage = source.index('-Name "01_plugins"')
    after_repair = source.rindex(marker)
    assert before_repair < repair_stage < after_repair


def test_source_binding_rejects_file_hash_drift(tmp_path: Path) -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    source_file = tmp_path / "bound.py"
    source_file.write_text("before", encoding="utf-8")
    snapshot_root = tmp_path / "source_snapshot"
    snapshot_root.mkdir()
    snapshot_file = snapshot_root / "bound.py"
    snapshot_file.write_text("before", encoding="utf-8")
    digest = __import__("hashlib").sha256(source_file.read_bytes()).hexdigest()
    binding_path = tmp_path / "SOURCE_BINDING.json"
    binding = {
        "schema_version": 2,
        "snapshot_root": str(snapshot_root),
        "files": [
            {
                "path": str(source_file),
                "repository": str(tmp_path),
                "relative_path": "bound.py",
                "bytes": source_file.stat().st_size,
                "sha256": digest,
                "snapshot_path": str(snapshot_file),
            }
        ],
        "repositories": [],
    }
    binding_path.write_text(json.dumps(binding), encoding="utf-8")
    binding_hash = __import__("hashlib").sha256(binding_path.read_bytes()).hexdigest()
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{runner}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($name in @('Get-FileSha256Lower', 'Assert-RepairSourceBinding')) {{
    $function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }}, $true) | Select-Object -First 1
    Invoke-Expression $function.Extent.Text
}}
function git {{ $global:LASTEXITCODE = 0; return 'bound.py' }}
$first = $true
try {{ Assert-RepairSourceBinding -BindingPath '{binding_path}' -ExpectedBindingSha256 '{binding_hash}' }} catch {{ $first = $false }}
Set-Content -LiteralPath '{source_file}' -Value 'after' -NoNewline
$secondRejected = $false
try {{ Assert-RepairSourceBinding -BindingPath '{binding_path}' -ExpectedBindingSha256 '{binding_hash}' }} catch {{ $secondRejected = $true }}
[pscustomobject]@{{ first_passed = $first; second_rejected = $secondRejected }} | ConvertTo-Json -Compress
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(completed.stdout.strip()) == {"first_passed": True, "second_rejected": True}


def test_source_binding_builds_and_monitors_immutable_execution_snapshot(tmp_path: Path) -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    workspace = tmp_path / "workspace"
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True)
    source_file = scripts / "bound.py"
    source_file.write_text("print('bound')\n", encoding="utf-8")
    binding_path = tmp_path / "SOURCE_BINDING.json"
    snapshot_root = tmp_path / "source_snapshot"
    command = rf'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{runner}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($name in @('Get-FileSha256Lower', 'New-RepairSourceBinding', 'Assert-RepairSourceSnapshot')) {{
    $function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }}, $true) | Select-Object -First 1
    Invoke-Expression $function.Extent.Text
}}
function git {{
    $global:LASTEXITCODE = 0
    if ($args -contains 'rev-parse') {{ return '0123456789abcdef0123456789abcdef01234567' }}
    if ($args -contains 'status') {{ return '' }}
    return 'scripts/bound.py'
}}
$bindingHash = New-RepairSourceBinding -Paths @('{source_file}') -OutputPath '{binding_path}' -RepositoryPaths @('{workspace}') -SnapshotRoot '{snapshot_root}' -WorkspaceRoot '{workspace}'
$binding = Get-Content -LiteralPath '{binding_path}' -Raw | ConvertFrom-Json
$firstPassed = $true
try {{ Assert-RepairSourceSnapshot -BindingPath '{binding_path}' -ExpectedBindingSha256 $bindingHash }} catch {{ $firstPassed = $false }}
(Get-Item -LiteralPath ([string]$binding.files[0].snapshot_path)).IsReadOnly = $false
Set-Content -LiteralPath ([string]$binding.files[0].snapshot_path) -Value 'drift' -NoNewline
$driftRejected = $false
try {{ Assert-RepairSourceSnapshot -BindingPath '{binding_path}' -ExpectedBindingSha256 $bindingHash }} catch {{ $driftRejected = $true }}
[pscustomobject]@{{
    schema = [int]$binding.schema_version
    snapshot_exists = Test-Path -LiteralPath ([string]$binding.files[0].snapshot_path)
    first_passed = $firstPassed
    drift_rejected = $driftRejected
}} | ConvertTo-Json -Compress
'''
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(completed.stdout.strip()) == {
        "schema": 2,
        "snapshot_exists": True,
        "first_passed": True,
        "drift_rejected": True,
    }


def test_active_lock_switch_keeps_old_lock_until_atomic_replacement(tmp_path: Path) -> None:
    runner = Path(__file__).resolve().parents[2] / "scripts" / "repair_all_codex_after_exit.ps1"
    lock_path = tmp_path / "active.lock.json"
    archive_path = tmp_path / "old" / "recovered_runner_lock.json"
    lock_path.write_text('{"run_id":"old","run_root":"old-root"}', encoding="utf-8")
    command = rf'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('{runner}', [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) {{ throw ($errors | Out-String) }}
foreach ($name in @('Get-FileSha256Lower', 'Write-Utf8FileDurable', 'Switch-ActiveRepairLock')) {{
    $function = $ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }}, $true) | Select-Object -First 1
    if ($null -eq $function) {{ throw "missing function $name" }}
    Invoke-Expression $function.Extent.Text
}}
$newPayload = [ordered]@{{ run_id = 'new'; run_root = 'new-root' }}
$failed = $false
try {{
    Switch-ActiveRepairLock -LockPath '{lock_path}' -ArchivePath '{archive_path}' -ExpectedRunId 'old' -ExpectedRunRoot 'old-root' -NewPayload $newPayload -BeforeAtomicReplace {{ throw 'injected failure' }}
}}
catch {{ $failed = $true }}
$afterFailure = Get-Content -LiteralPath '{lock_path}' -Raw | ConvertFrom-Json
Switch-ActiveRepairLock -LockPath '{lock_path}' -ArchivePath '{archive_path}' -ExpectedRunId 'old' -ExpectedRunRoot 'old-root' -NewPayload $newPayload
$afterSuccess = Get-Content -LiteralPath '{lock_path}' -Raw | ConvertFrom-Json
$archived = Get-Content -LiteralPath '{archive_path}' -Raw | ConvertFrom-Json
[pscustomobject]@{{
    failed = $failed
    failure_run_id = [string]$afterFailure.run_id
    success_run_id = [string]$afterSuccess.run_id
    archived_run_id = [string]$archived.run_id
}} | ConvertTo-Json -Compress
'''
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(completed.stdout.strip()) == {
        "failed": True,
        "failure_run_id": "old",
        "success_run_id": "new",
        "archived_run_id": "old",
    }
