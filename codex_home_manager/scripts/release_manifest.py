from __future__ import annotations

import argparse
import base64
import http.cookiejar
import hashlib
import json
import os
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import xml.etree.ElementTree as element_tree
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
import psutil


release_signing_directory = Path(r"D:\Backup\codex_home_manager\release-signing")
local_artifact_names = (
    "codex-home-manager-local-win-x64.exe",
    "codex-home-manager-local-win-x64.zip",
)
public_bundle_name = "connector-release.json"
public_checksum_name = "SHA256SUMS.txt"
release_manifest_name = "release-manifest.json"
release_signature_name = "release-manifest.json.sig"
release_public_key_name = "release-signing-public-key.pem"
release_fingerprint_name = "release-signing-public-key.sha256"
github_metadata_names = (
    release_manifest_name,
    release_signature_name,
    release_public_key_name,
    release_fingerprint_name,
)
public_dist_root_names = frozenset({"favicon.svg", "index.html"})
public_dist_asset_suffixes = frozenset({".css", ".js", ".wasm"})
source_evidence_public_names = (
    "codex-home-manager-source.zip",
    "codex-home-manager-source.cdx.json",
    "source-ci-test-summary.md",
    "source-provenance-attestation.sigstore.json",
    "source-sbom-attestation.sigstore.json",
)
source_sbom_predicate_type = "https://cyclonedx.org/bom"
source_provenance_predicate_type = "https://slsa.dev/provenance/v1"


class ReleaseManifestError(RuntimeError):
    pass


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary_path.write_bytes(content)
    os.replace(temporary_path, path)


def normalized_zip_datetime(source_date_epoch: int) -> tuple[int, int, int, int, int, int]:
    try:
        timestamp = datetime.fromtimestamp(source_date_epoch, timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise ReleaseManifestError("SOURCE_DATE_EPOCH is outside the supported timestamp range") from error
    if timestamp.year < 1980 or timestamp.year > 2107:
        raise ReleaseManifestError("SOURCE_DATE_EPOCH must map to a ZIP timestamp between 1980 and 2107")
    return (timestamp.year, timestamp.month, timestamp.day, timestamp.hour, timestamp.minute, timestamp.second // 2 * 2)


def create_deterministic_zip(source_directory: Path, archive_path: Path, *, source_date_epoch: int) -> None:
    if not source_directory.is_dir():
        raise ReleaseManifestError(f"ZIP source directory does not exist: {source_directory}")
    timestamp = normalized_zip_datetime(source_date_epoch)
    paths = sorted(source_directory.rglob("*"), key=lambda path: path.relative_to(source_directory).as_posix())
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = archive_path.with_name(f".{archive_path.name}.{os.getpid()}.tmp")
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in paths:
                relative_path = path.relative_to(source_directory).as_posix()
                is_directory = path.is_dir()
                entry_name = relative_path + ("/" if is_directory else "")
                info = zipfile.ZipInfo(entry_name, date_time=timestamp)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.flag_bits = 0x800
                executable = path.suffix.lower() in {".cmd", ".exe", ".ps1"}
                permissions = 0o755 if is_directory or executable else 0o644
                info.external_attr = (permissions & 0xFFFF) << 16
                info.external_attr |= 0x10 if is_directory else 0
                archive.writestr(info, b"" if is_directory else path.read_bytes())
        os.replace(temporary_path, archive_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def compare_reproducible_builds(
    first_dist_directory: Path,
    second_dist_directory: Path,
    first_executable_path: Path,
    second_executable_path: Path,
    first_archive_path: Path,
    second_archive_path: Path,
) -> None:
    if file_records(first_dist_directory) != file_records(second_dist_directory):
        raise ReleaseManifestError("dist reproducibility mismatch")
    if first_executable_path.read_bytes() != second_executable_path.read_bytes():
        raise ReleaseManifestError("EXE reproducibility mismatch")
    if first_archive_path.read_bytes() != second_archive_path.read_bytes():
        raise ReleaseManifestError("ZIP reproducibility mismatch")


def normalize_pyinstaller_executable(path: Path, *, source_date_epoch: int) -> None:
    content = bytearray(path.read_bytes())
    if len(content) < 0x40 or content[:2] != b"MZ":
        raise ReleaseManifestError(f"PyInstaller executable is not a PE file: {path}")
    pe_offset = struct.unpack_from("<I", content, 0x3C)[0]
    if pe_offset + 24 > len(content) or content[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ReleaseManifestError(f"PyInstaller executable has an invalid PE header: {path}")
    if source_date_epoch < 0 or source_date_epoch > 0xFFFFFFFF:
        raise ReleaseManifestError("SOURCE_DATE_EPOCH does not fit the PE timestamp field")
    struct.pack_into("<I", content, pe_offset + 8, source_date_epoch)
    optional_header_offset = pe_offset + 24
    optional_header_size = struct.unpack_from("<H", content, pe_offset + 20)[0]
    if optional_header_offset + optional_header_size > len(content) or optional_header_size < 68:
        raise ReleaseManifestError(f"PyInstaller executable has a truncated optional header: {path}")
    struct.pack_into("<I", content, optional_header_offset + 64, 0)

    optional_header_magic = struct.unpack_from("<H", content, optional_header_offset)[0]
    if optional_header_magic == 0x10B:
        data_directory_offset = optional_header_offset + 96
        number_of_directories_offset = optional_header_offset + 92
    elif optional_header_magic == 0x20B:
        data_directory_offset = optional_header_offset + 112
        number_of_directories_offset = optional_header_offset + 108
    else:
        raise ReleaseManifestError(f"PyInstaller executable has an unsupported PE optional header: {path}")

    number_of_directories = struct.unpack_from("<I", content, number_of_directories_offset)[0]
    debug_directory_index = 6
    if number_of_directories > debug_directory_index:
        debug_directory_record_offset = data_directory_offset + (debug_directory_index * 8)
        if debug_directory_record_offset + 8 > optional_header_offset + optional_header_size:
            raise ReleaseManifestError(f"PyInstaller executable has a truncated data directory: {path}")
        debug_rva, debug_size = struct.unpack_from("<II", content, debug_directory_record_offset)
        if debug_rva or debug_size:
            if not debug_rva or debug_size < 28 or debug_size % 28:
                raise ReleaseManifestError(f"PyInstaller executable has an invalid debug directory: {path}")
            section_count = struct.unpack_from("<H", content, pe_offset + 6)[0]
            section_table_offset = optional_header_offset + optional_header_size
            debug_file_offset = None
            for section_index in range(section_count):
                section_offset = section_table_offset + (section_index * 40)
                if section_offset + 40 > len(content):
                    raise ReleaseManifestError(f"PyInstaller executable has a truncated section table: {path}")
                virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from(
                    "<IIII", content, section_offset + 8
                )
                if virtual_address <= debug_rva < virtual_address + max(virtual_size, raw_size):
                    debug_file_offset = raw_pointer + (debug_rva - virtual_address)
                    break
            if debug_file_offset is None or debug_file_offset + debug_size > len(content):
                raise ReleaseManifestError(f"PyInstaller executable debug directory is outside its sections: {path}")
            for entry_offset in range(debug_file_offset, debug_file_offset + debug_size, 28):
                struct.pack_into("<I", content, entry_offset + 4, source_date_epoch)
    write_bytes(path, bytes(content))


def validate_github_release_payload(
    release: Any,
    *,
    expected_repository: str,
    expected_tag: str,
    expected_release_id: int,
    expected_asset_names: set[str],
    require_draft: bool,
) -> dict[str, Any]:
    if not isinstance(release, dict):
        raise ReleaseManifestError("GitHub release payload is invalid")
    if release.get("id") != expected_release_id or release.get("tag_name") != expected_tag:
        raise ReleaseManifestError("GitHub release ID or tag mismatch")
    if release.get("draft") is not require_draft or release.get("prerelease") is not False:
        raise ReleaseManifestError("GitHub release publication state mismatch")
    expected_html_url = f"https://github.com/{expected_repository}/releases/tag/{expected_tag}"
    if release.get("html_url") != expected_html_url:
        raise ReleaseManifestError("GitHub release repository URL mismatch")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ReleaseManifestError("GitHub release assets are missing")
    asset_names = [asset.get("name") for asset in assets if isinstance(asset, dict)]
    if len(asset_names) != len(assets) or len(set(asset_names)) != len(asset_names) or set(asset_names) != expected_asset_names:
        raise ReleaseManifestError("GitHub release asset set mismatch")
    normalized_assets = []
    for asset in assets:
        name = asset.get("name")
        size = asset.get("size")
        download_url = asset.get("browser_download_url")
        parsed_download_url = urlparse(download_url) if isinstance(download_url, str) else None
        expected_download_prefix = f"/{expected_repository}/releases/download/"
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(size, int)
            or size < 0
            or not isinstance(download_url, str)
            or parsed_download_url is None
            or parsed_download_url.scheme != "https"
            or parsed_download_url.hostname != "github.com"
            or not parsed_download_url.path.startswith(expected_download_prefix)
        ):
            raise ReleaseManifestError("GitHub release has an invalid asset")
        normalized_assets.append({"name": name, "size": size, "browser_download_url": download_url})
    return {
        "id": expected_release_id,
        "repository": expected_repository,
        "tag": expected_tag,
        "html_url": expected_html_url,
        "draft": require_draft,
        "prerelease": False,
        "assets": sorted(normalized_assets, key=lambda asset: asset["name"]),
    }


def random_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def http_probe(
    opener: Any,
    url: str,
    *,
    origin: str | None = None,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, bytes]:
    headers = {"Accept": "application/json"}
    if origin:
        headers["Origin"] = origin
    if token:
        headers["X-Codex-Manager-Token"] = token
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(url, data=body, headers=headers, method="POST" if payload is not None else "GET")
    try:
        with opener.open(request, timeout=5) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def terminate_verified_loopback_listener(port: int, executable_path: Path) -> None:
    listener_pids = {
        connection.pid
        for connection in psutil.net_connections(kind="tcp")
        if connection.pid
        and connection.status == psutil.CONN_LISTEN
        and connection.laddr
        and connection.laddr.port == port
        and connection.laddr.ip in {"127.0.0.1", "::1"}
    }
    for pid in listener_pids:
        try:
            listener_process = psutil.Process(pid)
            listener_name = listener_process.name()
            listener_executable = listener_process.exe()
        except (psutil.AccessDenied, psutil.NoSuchProcess) as error:
            raise ReleaseManifestError(f"cannot verify the random-port listener process {pid}") from error
        if (
            listener_name.casefold() != executable_path.name.casefold()
            and os.path.normcase(listener_executable) != os.path.normcase(str(executable_path.resolve()))
        ):
            raise ReleaseManifestError(f"refusing to terminate an unexpected process on black-box port {port}: {pid}")
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def wait_for_file_release(path: Path, *, timeout_seconds: float = 15) -> None:
    deadline = time.monotonic() + timeout_seconds
    probe_path = path.with_name(f".{path.name}.release-probe")
    while path.exists():
        try:
            os.replace(path, probe_path)
            os.replace(probe_path, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise ReleaseManifestError(f"final executable did not release its test file handle: {path}")
            time.sleep(0.1)


def blackbox_test_executable(executable_path: Path, *, port: int | None = None) -> dict[str, Any]:
    if not executable_path.is_file():
        raise ReleaseManifestError(f"final executable does not exist: {executable_path}")
    selected_port = port or random_loopback_port()
    base_url = f"http://127.0.0.1:{selected_port}"
    public_origin = "https://codex-home-manager.simplezion.com"
    with tempfile.TemporaryDirectory(prefix="codex-home-manager-blackbox-") as temporary_directory:
        codex_home = Path(temporary_directory) / ".codex"
        codex_home.mkdir()
        (codex_home / "AGENTS.md").write_text("black-box fixture\n", encoding="utf-8")
        for directory_name in ("sessions", "memories", "skills"):
            (codex_home / directory_name).mkdir()
        connection = sqlite3.connect(codex_home / "state_5.sqlite")
        try:
            connection.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL, source TEXT NOT NULL, model_provider TEXT NOT NULL,
                    cwd TEXT NOT NULL, title TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
                    approval_mode TEXT NOT NULL, tokens_used INTEGER NOT NULL DEFAULT 0,
                    has_user_event INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
                    archived_at INTEGER, git_sha TEXT, git_branch TEXT, git_origin_url TEXT,
                    cli_version TEXT NOT NULL DEFAULT '', first_user_message TEXT NOT NULL DEFAULT '',
                    agent_nickname TEXT, agent_role TEXT, memory_mode TEXT NOT NULL DEFAULT 'enabled',
                    model TEXT, reasoning_effort TEXT, agent_path TEXT, created_at_ms INTEGER,
                    updated_at_ms INTEGER, thread_source TEXT, preview TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.execute(
                "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT NOT NULL, child_thread_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
            )
            connection.commit()
        finally:
            connection.close()
        environment = os.environ.copy()
        environment.update(
            {
                "CODEX_HOME": str(codex_home),
                "CODEX_HOME_MANAGER_PORT": str(selected_port),
                "CODEX_HOME_MANAGER_NO_BROWSER": "1",
                "CODEX_HOME_MANAGER_SKIP_PROTOCOL": "1",
                "CODEX_HOME_MANAGER_BACKUP_ROOT": str(Path(temporary_directory) / "backups"),
                "CODEX_HOME_MANAGER_EXPORT_ROOT": str(Path(temporary_directory) / "exports"),
            }
        )
        process = subprocess.Popen(
            [str(executable_path)],
            cwd=temporary_directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            plain_opener = build_opener()
            deadline = time.monotonic() + 45
            while True:
                if process.poll() is not None:
                    raise ReleaseManifestError(f"final executable exited before readiness with code {process.returncode}")
                try:
                    status, _ = http_probe(plain_opener, f"{base_url}/api/capabilities")
                    if status == 200:
                        break
                except (URLError, TimeoutError, OSError):
                    pass
                if time.monotonic() >= deadline:
                    raise ReleaseManifestError("final executable did not become ready on its random loopback port")
                time.sleep(0.25)

            public_checks = (
                ("snapshot", f"{base_url}/api/snapshot", None),
                ("home overview", f"{base_url}/api/home/overview", None),
                ("resource read", f"{base_url}/api/resources/read?relative_path=AGENTS.md", None),
                ("auth token", f"{base_url}/api/auth/token", None),
                (
                    "preview",
                    f"{base_url}/api/resources/write/preview",
                    {"relativePath": "AGENTS.md", "content": "blocked", "createParentDirectories": True},
                ),
            )
            rejected = {}
            for name, url, payload in public_checks:
                status, _ = http_probe(plain_opener, url, origin=public_origin, payload=payload)
                if status not in {401, 403}:
                    raise ReleaseManifestError(f"public Origin was not rejected for {name}: HTTP {status}")
                rejected[name] = status

            local_opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
            status, token_bytes = http_probe(local_opener, f"{base_url}/api/auth/token", origin=base_url)
            if status != 200:
                raise ReleaseManifestError(f"same-origin loopback authorization failed: HTTP {status}")
            try:
                token = json.loads(token_bytes.decode("utf-8"))["token"]
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
                raise ReleaseManifestError("same-origin loopback authorization returned no token") from error
            authorized_reads = {}
            for name, url in (
                ("snapshot", f"{base_url}/api/snapshot"),
                ("home overview", f"{base_url}/api/home/overview"),
                ("resource read", f"{base_url}/api/resources/read?relative_path=AGENTS.md"),
            ):
                status, _ = http_probe(local_opener, url, origin=base_url, token=token)
                if status != 200:
                    raise ReleaseManifestError(f"same-origin authorized {name} failed: HTTP {status}")
                authorized_reads[name] = status
            return {
                "port": selected_port,
                "public_origin": public_origin,
                "public_rejections": rejected,
                "same_origin_authorized_reads": authorized_reads,
            }
        finally:
            terminate_process_tree(process)
            terminate_verified_loopback_listener(selected_port, executable_path)
            wait_for_file_release(codex_home / "state_5.sqlite")


def run_git(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", None) or str(error)
        raise ReleaseManifestError(f"git failed for {repository}: {detail.strip()}") from error
    return result.stdout.strip()


def repository_state(name: str, repository: Path, require_clean: bool = True) -> dict[str, Any]:
    repository = repository.resolve()
    git_root = Path(run_git(repository, "rev-parse", "--show-toplevel")).resolve()
    if os.path.normcase(git_root) != os.path.normcase(repository):
        raise ReleaseManifestError(f"{name} repository path is not its Git root: {repository}")
    state = {
        "head": run_git(repository, "rev-parse", "HEAD"),
        "branch": run_git(repository, "branch", "--show-current"),
    }
    clean = not run_git(repository, "status", "--porcelain=v1", "--untracked-files=all")
    state["clean"] = clean
    if require_clean and not clean:
        raise ReleaseManifestError(f"{name} repository is not clean")
    return state


def require_main_branch(name: str, state: dict[str, Any]) -> None:
    if state.get("branch") != "main":
        raise ReleaseManifestError(f"{name} repository must be on main")


def validate_private_key_path(private_key_path: Path) -> Path:
    allowed_directory = release_signing_directory.resolve()
    resolved_path = private_key_path.resolve()
    if resolved_path.parent != allowed_directory:
        raise ReleaseManifestError(f"private key must be located directly in {allowed_directory}")
    return resolved_path


def validate_trusted_fingerprint_path(fingerprint_path: Path) -> Path:
    allowed_directory = release_signing_directory.resolve()
    resolved_path = fingerprint_path.resolve()
    if resolved_path.parent != allowed_directory:
        raise ReleaseManifestError(f"trusted public key fingerprint must be located directly in {allowed_directory}")
    return resolved_path


def public_key_fingerprint_from_key(public_key: Ed25519PublicKey) -> str:
    der_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return f"sha256:{sha256_bytes(der_bytes)}"


def load_public_key(public_key_path: Path) -> Ed25519PublicKey:
    try:
        public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
    except (OSError, ValueError) as error:
        raise ReleaseManifestError("release public key is unreadable") from error
    if not isinstance(public_key, Ed25519PublicKey):
        raise ReleaseManifestError("release public key is not an Ed25519 key")
    return public_key


def normalize_fingerprint(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized.startswith("sha256:") or len(normalized) != len("sha256:") + 64:
        raise ReleaseManifestError("trusted public key fingerprint must be sha256:<64 lowercase hex characters>")
    if any(character not in "0123456789abcdef" for character in normalized.removeprefix("sha256:")):
        raise ReleaseManifestError("trusted public key fingerprint must be sha256:<64 lowercase hex characters>")
    return normalized


def load_trusted_public_key_fingerprint(fingerprint_path: Path) -> str:
    fingerprint_path = validate_trusted_fingerprint_path(fingerprint_path)
    if not fingerprint_path.is_file():
        raise ReleaseManifestError(f"trusted public key fingerprint does not exist: {fingerprint_path}")
    try:
        return normalize_fingerprint(fingerprint_path.read_text(encoding="ascii"))
    except OSError as error:
        raise ReleaseManifestError(f"cannot read trusted public key fingerprint: {fingerprint_path}") from error


def assert_public_key_matches_trust(public_key: Ed25519PublicKey, trusted_fingerprint: str) -> None:
    actual_fingerprint = public_key_fingerprint_from_key(public_key)
    if actual_fingerprint != trusted_fingerprint:
        raise ReleaseManifestError("public key fingerprint mismatch")


def validate_public_site_key_pin(site_directory: Path, trusted_fingerprint: str) -> None:
    public_key = load_public_key(site_directory / "release-signing-public-key.pem")
    assert_public_key_matches_trust(public_key, trusted_fingerprint)
    fingerprint_path = site_directory / "release-signing-public-key.sha256"
    try:
        published_fingerprint = normalize_fingerprint(fingerprint_path.read_text(encoding="ascii"))
    except OSError as error:
        raise ReleaseManifestError(f"public release key fingerprint does not exist: {fingerprint_path}") from error
    if published_fingerprint != trusted_fingerprint:
        raise ReleaseManifestError("public release key fingerprint does not match private-root trust")


def generate_key_pair(
    private_key_path: Path,
    public_key_path: Path,
    trusted_public_key_fingerprint_path: Path,
) -> None:
    private_key_path = validate_private_key_path(private_key_path)
    trusted_public_key_fingerprint_path = validate_trusted_fingerprint_path(trusted_public_key_fingerprint_path)
    if private_key_path.exists():
        try:
            private_key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
        except (OSError, ValueError) as error:
            raise ReleaseManifestError("release private key is unreadable") from error
        if not isinstance(private_key, Ed25519PrivateKey):
            raise ReleaseManifestError("release private key is not an Ed25519 key")
    else:
        private_key_path.parent.mkdir(parents=True, exist_ok=True)
        private_key = Ed25519PrivateKey.generate()
        write_bytes(
            private_key_path,
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
        )
        os.chmod(private_key_path, 0o600)

    public_key = private_key.public_key()
    trusted_fingerprint = public_key_fingerprint_from_key(public_key)
    if trusted_public_key_fingerprint_path.exists():
        if load_trusted_public_key_fingerprint(trusted_public_key_fingerprint_path) != trusted_fingerprint:
            raise ReleaseManifestError("existing trusted public key fingerprint does not match release key")
    else:
        write_bytes(trusted_public_key_fingerprint_path, (trusted_fingerprint + "\n").encode("ascii"))
    write_bytes(
        public_key_path,
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )


def capture_build_source_state(output_path: Path, *, root_repository: Path, manager_repository: Path) -> None:
    sources = {
        "root": repository_state("root", root_repository),
        "manager": repository_state("manager", manager_repository),
    }
    snapshot = {
        "schema_version": 2,
        "phase": "prebuild",
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sources": sources,
        "repository_paths": {
            "root": str(root_repository.resolve()),
            "manager": str(manager_repository.resolve()),
        },
    }
    write_bytes(output_path, canonical_json_bytes(snapshot))


def capture_artifact_public_source_state(
    output_path: Path,
    *,
    public_repository: Path,
    artifact_deployment_id: str,
) -> None:
    if not artifact_deployment_id.strip():
        raise ReleaseManifestError("artifact deployment ID is required")
    public_state = repository_state("public", public_repository)
    require_main_branch("public", public_state)
    snapshot = {
        "schema_version": 2,
        "phase": "artifact-public",
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sources": {"artifact_public": public_state},
        "repository_paths": {"artifact_public": str(public_repository.resolve())},
        "artifact_deployment_id": artifact_deployment_id.strip(),
    }
    write_bytes(output_path, canonical_json_bytes(snapshot))


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseManifestError(f"cannot read JSON file {path}: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseManifestError(f"JSON file must contain an object: {path}")
    return value


def validate_repository_name(value: str, label: str) -> str:
    parts = value.split("/")
    if (
        len(parts) != 2
        or any(
            not part
            or re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None
            for part in parts
        )
    ):
        raise ReleaseManifestError(f"{label} is invalid")
    return value


def validate_signer_workflow(repository: str, value: str) -> str:
    prefix = f"github.com/{repository}/.github/workflows/"
    relative_path = value.removeprefix(prefix)
    if (
        not value.startswith(prefix)
        or not relative_path
        or ".." in Path(relative_path).parts
        or re.fullmatch(r"[A-Za-z0-9._/-]+\.ya?ml", relative_path) is None
    ):
        raise ReleaseManifestError("source evidence signer workflow is invalid")
    return value


def verify_github_attestation(
    *,
    subject_path: Path,
    bundle_path: Path,
    repository: str,
    signer_workflow: str,
    source_commit: str,
    predicate_type: str,
) -> None:
    command = [
        "gh",
        "attestation",
        "verify",
        str(subject_path),
        "--bundle",
        str(bundle_path),
        "--repo",
        repository,
        "--signer-workflow",
        signer_workflow,
        "--source-digest",
        source_commit,
        "--source-ref",
        "refs/heads/source",
        "--predicate-type",
        predicate_type,
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8")
    except OSError as error:
        raise ReleaseManifestError("GitHub attestation verification could not start") from error
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "gh returned a non-zero exit code"
        raise ReleaseManifestError(f"GitHub attestation verification failed: {detail}")
    try:
        verification_results = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseManifestError("GitHub attestation verification returned invalid JSON") from error
    if not isinstance(verification_results, list) or not verification_results:
        raise ReleaseManifestError("GitHub attestation verification returned no verified attestations")


def load_source_evidence_checksums(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise ReleaseManifestError("source evidence SHA256SUMS is unreadable") from error
    entries: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        match = re.fullmatch(r"([0-9a-f]{64}) \*evidence/([^/\\]+)", line)
        if match is None or match.group(2) in entries:
            raise ReleaseManifestError(f"invalid source evidence SHA256SUMS line {line_number}")
        entries[match.group(2)] = match.group(1)
    return entries


def assert_public_evidence_privacy(name: str, content: bytes) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReleaseManifestError(f"public source evidence privacy policy requires UTF-8 text: {name}") from error
    blocked_patterns = (
        r"(?i)[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"'<>]+",
        r"(?i)[A-Z]:[\\/]+(?:\.codex|ZionCloudDrive)[\\/]",
        r"(?i)/(?:Users|home)/(?!runner(?:/|$))[^/\s\"'<>]+/",
        r"(?i)\b(?:gh[opsu]_|github_pat_|sk-|cfpat-)[A-Za-z0-9_-]{12,}",
        r"(?i)\b(?:CLOUDFLARE_API_TOKEN|GITHUB_TOKEN|CODEX_HOME_MANAGER_WRITE_TOKEN)\s*[:=]",
        r"(?i)<(?:user|assistant|system|developer)>\s*",
    )
    if any(re.search(pattern, text) is not None for pattern in blocked_patterns):
        raise ReleaseManifestError(f"public source evidence violates privacy policy: {name}")


def load_source_commits_from_archive(archive_path: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = [info.filename for info in archive.infolist()]
            if len(names) != len(set(names)):
                raise ReleaseManifestError("source archive contains duplicate entries")
            for name in names:
                path = Path(name.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts or name.startswith(("/", "\\")):
                    raise ReleaseManifestError("source archive contains an unsafe entry")
            if names.count("SOURCE_COMMITS.json") != 1:
                raise ReleaseManifestError("source archive must contain exactly one SOURCE_COMMITS.json")
            source_commits_bytes = archive.read("SOURCE_COMMITS.json")
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise ReleaseManifestError("source archive is invalid") from error
    assert_public_evidence_privacy("SOURCE_COMMITS.json", source_commits_bytes)
    try:
        source_commits = json.loads(source_commits_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseManifestError("source archive SOURCE_COMMITS.json is invalid") from error
    sources = source_commits.get("sources") if isinstance(source_commits, dict) else None
    if source_commits.get("schemaVersion") != 2 or not isinstance(sources, dict):
        raise ReleaseManifestError("source archive SOURCE_COMMITS.json has an unsupported schema")
    result: dict[str, str] = {}
    for manifest_name, output_name in (("rootRepository", "root"), ("managerRepository", "manager")):
        source = sources.get(manifest_name)
        commit = source.get("commit") if isinstance(source, dict) else None
        if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise ReleaseManifestError(f"source archive has an invalid {manifest_name} commit")
        result[output_name] = commit
    return result


def load_passing_junit(path: Path) -> dict[str, int | float]:
    try:
        root = element_tree.parse(path).getroot()
    except (OSError, element_tree.ParseError) as error:
        raise ReleaseManifestError("source evidence JUnit report is invalid") from error
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        raise ReleaseManifestError("source evidence JUnit report has no test suites")
    try:
        quality: dict[str, int | float] = {
            key: sum(int(suite.attrib.get(key, "0")) for suite in suites)
            for key in ("tests", "failures", "errors", "skipped")
        }
        quality["pytest_seconds"] = sum(float(suite.attrib.get("time", "0")) for suite in suites)
    except ValueError as error:
        raise ReleaseManifestError("source evidence JUnit totals are invalid") from error
    if quality["tests"] < 1 or quality["failures"] != 0 or quality["errors"] != 0:
        raise ReleaseManifestError("source evidence JUnit report does not prove a passing CI run")
    return quality


def source_test_summary_bytes(quality: dict[str, int | float]) -> bytes:
    return (
        "# Source CI test summary\n\n"
        f"- Tests: {quality['tests']}\n"
        f"- Failures: {quality['failures']}\n"
        f"- Errors: {quality['errors']}\n"
        f"- Skipped: {quality['skipped']}\n"
        f"- Pytest time: {quality['pytest_seconds']:.3f} seconds\n"
        "- Complete quality gate: passed\n"
    ).encode("utf-8")


def update_public_checksums(checksum_path: Path, records: list[dict[str, Any]]) -> None:
    existing = load_checksum_entries(checksum_path) if checksum_path.stat().st_size else {}
    for record in records:
        existing[record["name"]] = record["sha256"]
    content = "".join(f"{digest}  {name}\n" for name, digest in sorted(existing.items()))
    write_bytes(checksum_path, content.encode("ascii"))


def prepare_source_release_evidence(
    *,
    evidence_directory: Path,
    expected_source_commit: str,
    expected_build_sources: dict[str, dict[str, Any]],
    repository: str,
    signer_workflow: str,
    release_directory: Path,
    public_site_directory: Path,
    proof_path: Path,
    attestation_verifier: Callable[..., None] = verify_github_attestation,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", expected_source_commit) is None:
        raise ReleaseManifestError("expected source commit must be a lowercase 40-character Git commit")
    validate_repository_name(repository, "source evidence repository")
    validate_signer_workflow(repository, signer_workflow)
    if not evidence_directory.is_dir():
        raise ReleaseManifestError("source evidence directory does not exist")
    actual_paths = [path for path in evidence_directory.iterdir()]
    if any(not path.is_file() or path.is_symlink() for path in actual_paths):
        raise ReleaseManifestError("source evidence file set mismatch")
    actual_names = {path.name for path in actual_paths}
    archive_matches = [match for name in actual_names if (match := re.fullmatch(r"codex-home-manager-source-([0-9a-f]{40})\.zip", name))]
    sbom_matches = [match for name in actual_names if (match := re.fullmatch(r"codex-home-manager-source-([0-9a-f]{40})\.cdx\.json", name))]
    if len(archive_matches) != 1 or len(sbom_matches) != 1:
        raise ReleaseManifestError("source evidence file set mismatch")
    if archive_matches[0].group(1) != expected_source_commit or sbom_matches[0].group(1) != expected_source_commit:
        raise ReleaseManifestError("source evidence commit mismatch")
    archive_name = archive_matches[0].group(0)
    sbom_name = sbom_matches[0].group(0)
    checksum_subject_names = {
        archive_name,
        sbom_name,
        "junit.xml",
        "quality-gate.log",
        "quality-gate-status.txt",
        "test-summary.md",
    }
    expected_names = checksum_subject_names | {
        "SHA256SUMS.txt",
        "source-sbom-attestation.sigstore.json",
        "source-provenance-attestation.sigstore.json",
    }
    if actual_names != expected_names:
        raise ReleaseManifestError("source evidence file set mismatch")

    checksums = load_source_evidence_checksums(evidence_directory / "SHA256SUMS.txt")
    if set(checksums) != checksum_subject_names:
        raise ReleaseManifestError("source evidence SHA256SUMS subject set mismatch")
    for name in checksum_subject_names:
        if sha256_file(evidence_directory / name) != checksums[name]:
            raise ReleaseManifestError(f"source evidence SHA256 mismatch: {name}")

    if (evidence_directory / "quality-gate-status.txt").read_text(encoding="utf-8").strip() != "passed":
        raise ReleaseManifestError("source evidence quality gate did not pass")
    quality = load_passing_junit(evidence_directory / "junit.xml")
    generated_summary = source_test_summary_bytes(quality)
    original_summary = (evidence_directory / "test-summary.md").read_bytes().replace(b"\r\n", b"\n")
    if original_summary != generated_summary:
        raise ReleaseManifestError("source evidence test summary does not match JUnit and quality gate results")

    archive_path = evidence_directory / archive_name
    sbom_path = evidence_directory / sbom_name
    source_commits = load_source_commits_from_archive(archive_path)
    for name in ("root", "manager"):
        expected_state = expected_build_sources.get(name)
        if not isinstance(expected_state, dict) or source_commits[name] != expected_state.get("head"):
            raise ReleaseManifestError(f"source evidence {name} commit does not match build source")
    try:
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseManifestError("source evidence CycloneDX SBOM is invalid") from error
    if (
        sbom_path.stat().st_size > 16 * 1024 * 1024
        or not isinstance(sbom, dict)
        or sbom.get("bomFormat") != "CycloneDX"
        or not isinstance(sbom.get("specVersion"), str)
        or not isinstance(sbom.get("serialNumber"), str)
        or not sbom["serialNumber"].startswith("urn:uuid:")
    ):
        raise ReleaseManifestError("source evidence CycloneDX SBOM is invalid")
    sbom_bundle_path = evidence_directory / "source-sbom-attestation.sigstore.json"
    provenance_bundle_path = evidence_directory / "source-provenance-attestation.sigstore.json"
    for public_input in (sbom_path, sbom_bundle_path, provenance_bundle_path):
        content = public_input.read_bytes()
        assert_public_evidence_privacy(public_input.name, content)
        try:
            bundle_or_sbom = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReleaseManifestError(f"public source evidence JSON is invalid: {public_input.name}") from error
        if not isinstance(bundle_or_sbom, dict):
            raise ReleaseManifestError(f"public source evidence JSON must be an object: {public_input.name}")

    attestation_verifier(
        subject_path=archive_path,
        bundle_path=sbom_bundle_path,
        repository=repository,
        signer_workflow=signer_workflow,
        source_commit=expected_source_commit,
        predicate_type=source_sbom_predicate_type,
    )
    for name in sorted(checksum_subject_names):
        attestation_verifier(
            subject_path=evidence_directory / name,
            bundle_path=provenance_bundle_path,
            repository=repository,
            signer_workflow=signer_workflow,
            source_commit=expected_source_commit,
            predicate_type=source_provenance_predicate_type,
        )

    public_content = {
        "codex-home-manager-source.zip": archive_path.read_bytes(),
        "codex-home-manager-source.cdx.json": sbom_path.read_bytes(),
        "source-ci-test-summary.md": generated_summary,
        "source-provenance-attestation.sigstore.json": provenance_bundle_path.read_bytes(),
        "source-sbom-attestation.sigstore.json": sbom_bundle_path.read_bytes(),
    }
    release_directory.mkdir(parents=True, exist_ok=True)
    public_site_directory.mkdir(parents=True, exist_ok=True)
    assets: list[dict[str, Any]] = []
    for name in source_evidence_public_names:
        content = public_content[name]
        write_bytes(release_directory / name, content)
        write_bytes(public_site_directory / name, content)
        assets.append({"name": name, "sha256": sha256_bytes(content), "size": len(content)})
    update_public_checksums(public_site_directory / public_checksum_name, assets)
    proof = {
        "schema_version": 1,
        "source_commit": expected_source_commit,
        "source_ref": "refs/heads/source",
        "source_commits": source_commits,
        "repository": repository,
        "signer_workflow": signer_workflow,
        "attestations": {
            "verifier": "gh attestation verify",
            "deny_self_hosted_runners": True,
            "sbom_predicate_type": source_sbom_predicate_type,
            "provenance_predicate_type": source_provenance_predicate_type,
        },
        "quality": quality,
        "assets": assets,
    }
    write_bytes(proof_path, canonical_json_bytes(proof))
    return proof


def valid_source_state(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseManifestError(f"source snapshot has invalid {name} state")
    if value.get("clean") is not True or not isinstance(value.get("head"), str) or not value["head"].strip():
        raise ReleaseManifestError(f"source snapshot has invalid {name} state")
    if not isinstance(value.get("branch"), str):
        raise ReleaseManifestError(f"source snapshot has invalid {name} branch")
    return value


def load_build_source_snapshot(snapshot_path: Path, *, verify_current: bool) -> dict[str, dict[str, Any]]:
    snapshot = load_json_object(snapshot_path)
    if snapshot.get("schema_version") != 2 or snapshot.get("phase") != "prebuild":
        raise ReleaseManifestError("unsupported build source snapshot schema")
    sources = snapshot.get("sources")
    repository_paths = snapshot.get("repository_paths")
    if not isinstance(sources, dict) or not isinstance(repository_paths, dict):
        raise ReleaseManifestError("build source snapshot is incomplete")
    result: dict[str, dict[str, Any]] = {}
    for name in ("root", "manager"):
        expected = valid_source_state(name, sources.get(name))
        repository_path = repository_paths.get(name)
        if not isinstance(repository_path, str):
            raise ReleaseManifestError(f"build source snapshot has no {name} repository path")
        if verify_current:
            actual = repository_state(name, Path(repository_path), require_clean=True)
            if actual["head"] != expected["head"]:
                raise ReleaseManifestError(f"{name} HEAD changed after build source capture")
        result[name] = expected
    return result


def validate_build_source_state(snapshot_path: Path) -> None:
    load_build_source_snapshot(snapshot_path, verify_current=True)


def load_artifact_public_source_snapshot(snapshot_path: Path) -> tuple[dict[str, Any], str]:
    snapshot = load_json_object(snapshot_path)
    if snapshot.get("schema_version") != 2 or snapshot.get("phase") != "artifact-public":
        raise ReleaseManifestError("unsupported artifact public source snapshot schema")
    sources = snapshot.get("sources")
    repository_paths = snapshot.get("repository_paths")
    deployment_id = snapshot.get("artifact_deployment_id")
    if not isinstance(sources, dict) or not isinstance(repository_paths, dict) or not isinstance(deployment_id, str):
        raise ReleaseManifestError("artifact public source snapshot is incomplete")
    public_state = valid_source_state("artifact public", sources.get("artifact_public"))
    require_main_branch("artifact public", public_state)
    if not isinstance(repository_paths.get("artifact_public"), str) or not deployment_id.strip():
        raise ReleaseManifestError("artifact public source snapshot is incomplete")
    return public_state, deployment_id.strip()


def file_records(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        raise ReleaseManifestError(f"directory does not exist: {directory}")
    records = []
    for path in sorted((item for item in directory.rglob("*") if item.is_file()), key=lambda item: item.relative_to(directory).as_posix()):
        records.append({"path": path.relative_to(directory).as_posix(), "sha256": sha256_file(path), "size": path.stat().st_size})
    if not records:
        raise ReleaseManifestError(f"directory has no files: {directory}")
    return records


def validate_public_dist_relative_path(relative_path: str) -> str:
    normalized = Path(relative_path).as_posix()
    path = Path(normalized)
    if path.is_absolute() or ".." in path.parts or normalized != relative_path:
        raise ReleaseManifestError(f"file is outside the public dist allowlist: {relative_path}")
    if normalized in public_dist_root_names:
        return normalized
    if (
        len(path.parts) == 2
        and path.parts[0] == "assets"
        and re.fullmatch(r"[A-Za-z0-9._-]+", path.name) is not None
        and path.suffix.lower() in public_dist_asset_suffixes
        and path.name not in {".", ".."}
    ):
        return normalized
    raise ReleaseManifestError(f"file is outside the public dist allowlist: {relative_path}")


def public_dist_file_records(directory: Path) -> list[dict[str, Any]]:
    records = file_records(directory)
    for record in records:
        validate_public_dist_relative_path(record["path"])
        path = directory / record["path"]
        if path.is_symlink():
            raise ReleaseManifestError(f"public dist allowlist rejects symbolic links: {record['path']}")
    paths = {record["path"] for record in records}
    if "index.html" not in paths:
        raise ReleaseManifestError("public dist allowlist requires index.html")
    return records


def managed_public_site_dist_paths(site_directory: Path) -> list[str]:
    if not site_directory.is_dir():
        raise ReleaseManifestError(f"public site directory does not exist: {site_directory}")
    paths: list[str] = []
    for root_name in sorted(public_dist_root_names):
        path = site_directory / root_name
        if path.is_symlink():
            raise ReleaseManifestError(f"public site dist allowlist rejects symbolic links: {root_name}")
        if path.is_file():
            paths.append(root_name)
    assets_directory = site_directory / "assets"
    if assets_directory.is_symlink():
        raise ReleaseManifestError("public site dist allowlist rejects the assets symbolic link")
    if assets_directory.is_dir():
        for path in sorted(assets_directory.rglob("*"), key=lambda item: item.relative_to(site_directory).as_posix()):
            if not path.is_file() or path.suffix.lower() not in public_dist_asset_suffixes:
                continue
            relative_path = path.relative_to(site_directory).as_posix()
            validate_public_dist_relative_path(relative_path)
            if path.is_symlink():
                raise ReleaseManifestError(f"public site dist allowlist rejects symbolic links: {relative_path}")
            paths.append(relative_path)
    return sorted(paths)


def plan_public_site_dist_sync(dist_directory: Path, site_directory: Path) -> dict[str, Any]:
    copy_files = public_dist_file_records(dist_directory)
    expected_paths = {record["path"] for record in copy_files}
    stale_files = [path for path in managed_public_site_dist_paths(site_directory) if path not in expected_paths]
    return {
        "schema_version": 1,
        "copy_files": copy_files,
        "stale_files": stale_files,
    }


def verify_public_site_dist(dist_directory: Path, site_directory: Path) -> list[dict[str, Any]]:
    expected_records = public_dist_file_records(dist_directory)
    expected_paths = {record["path"] for record in expected_records}
    actual_paths = set(managed_public_site_dist_paths(site_directory))
    if actual_paths != expected_paths:
        raise ReleaseManifestError("public site dist file set mismatch")
    public_records: list[dict[str, Any]] = []
    for expected in expected_records:
        path = site_directory / expected["path"]
        if (
            not path.is_file()
            or path.stat().st_size != expected["size"]
            or sha256_file(path) != expected["sha256"]
        ):
            raise ReleaseManifestError(f"public site dist hash mismatch: {expected['path']}")
        public_records.append(record_for_path(path, expected["path"]))
    return public_records


def record_for_path(path: Path, relative_path: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise ReleaseManifestError(f"required release artifact does not exist: {path}")
    return {"path": relative_path or path.name, "sha256": sha256_file(path), "size": path.stat().st_size}


def load_checksum_entries(checksum_path: Path) -> dict[str, str]:
    try:
        lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ReleaseManifestError(f"cannot read checksum file: {checksum_path}") from error
    entries: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        if not line:
            continue
        parts = line.split("  ", maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64 or any(character not in "0123456789abcdef" for character in parts[0]):
            raise ReleaseManifestError(f"invalid SHA256SUMS line {line_number}")
        if not parts[1] or Path(parts[1]).name != parts[1] or parts[1] in entries:
            raise ReleaseManifestError(f"invalid SHA256SUMS entry: {parts[1]}")
        entries[parts[1]] = parts[0]
    return entries


def load_public_bundle(site_directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    bundle_path = site_directory / public_bundle_name
    bundle = load_json_object(bundle_path)
    if bundle.get("schemaVersion") != 1 or not isinstance(bundle.get("version"), str):
        raise ReleaseManifestError("public artifact bundle has an invalid schema")
    artifacts = bundle.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise ReleaseManifestError("public artifact bundle must contain exactly the EXE and ZIP")
    records: list[dict[str, Any]] = []
    kinds: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise ReleaseManifestError("public artifact bundle has an invalid artifact")
        name = artifact.get("name")
        expected_hash = artifact.get("sha256")
        expected_size = artifact.get("size")
        kind = artifact.get("kind")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
            or not isinstance(expected_size, int)
            or kind not in {"exe", "zip"}
            or kind in kinds
        ):
            raise ReleaseManifestError("public artifact bundle has an invalid artifact")
        path = site_directory / name
        if not path.is_file() or path.stat().st_size != expected_size or sha256_file(path) != expected_hash:
            raise ReleaseManifestError(f"public artifact bundle hash mismatch: {name}")
        kinds.add(kind)
        records.append(record_for_path(path))
    if kinds != {"exe", "zip"}:
        raise ReleaseManifestError("public artifact bundle must contain exactly the EXE and ZIP")
    return bundle, records


def validate_public_bundle_matches_local_artifacts(
    bundle: dict[str, Any], local_records: list[dict[str, Any]]
) -> None:
    local_hashes = {record["sha256"] for record in local_records}
    artifact_hashes = {artifact.get("sha256") for artifact in bundle["artifacts"] if isinstance(artifact, dict)}
    if local_hashes != artifact_hashes:
        raise ReleaseManifestError("public artifact bundle does not match local release artifacts")


def collect_public_artifact_records(
    site_directory: Path,
    local_records: list[dict[str, Any]],
    source_evidence: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    bundle, bundle_records = load_public_bundle(site_directory)
    validate_public_bundle_matches_local_artifacts(bundle, local_records)
    checksum_path = site_directory / public_checksum_name
    checksum_entries = load_checksum_entries(checksum_path)
    for record in [*bundle_records, record_for_path(site_directory / public_bundle_name)]:
        if checksum_entries.get(record["path"]) != record["sha256"]:
            raise ReleaseManifestError(f"SHA256SUMS mismatch for {record['path']}")
    source_records: list[dict[str, Any]] = []
    if source_evidence is not None:
        for asset in source_evidence["assets"]:
            path = site_directory / asset["name"]
            record = record_for_path(path)
            if record["sha256"] != asset["sha256"] or record["size"] != asset["size"]:
                raise ReleaseManifestError(f"public source evidence hash mismatch: {asset['name']}")
            if checksum_entries.get(asset["name"]) != asset["sha256"]:
                raise ReleaseManifestError(f"SHA256SUMS mismatch for {asset['name']}")
            source_records.append(record)
    return sorted(
        [
            *bundle_records,
            *source_records,
            record_for_path(checksum_path),
            record_for_path(site_directory / public_bundle_name),
        ],
        key=lambda item: item["path"],
    )


def load_source_evidence_proof(
    proof_path: Path,
    *,
    build_sources: dict[str, dict[str, Any]],
    release_directory: Path,
    public_site_directory: Path,
) -> dict[str, Any]:
    proof = load_json_object(proof_path)
    if proof.get("schema_version") != 1:
        raise ReleaseManifestError("source evidence proof has an unsupported schema")
    source_commit = proof.get("source_commit")
    source_ref = proof.get("source_ref")
    source_commits = proof.get("source_commits")
    repository = proof.get("repository")
    signer_workflow = proof.get("signer_workflow")
    attestations = proof.get("attestations")
    quality = proof.get("quality")
    assets = proof.get("assets")
    if (
        not isinstance(source_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        or source_ref != "refs/heads/source"
        or not isinstance(source_commits, dict)
        or not isinstance(repository, str)
        or not isinstance(signer_workflow, str)
        or not isinstance(attestations, dict)
        or not isinstance(quality, dict)
        or not isinstance(assets, list)
    ):
        raise ReleaseManifestError("source evidence proof is incomplete")
    validate_repository_name(repository, "source evidence repository")
    validate_signer_workflow(repository, signer_workflow)
    if source_commits != {name: build_sources[name]["head"] for name in ("root", "manager")}:
        raise ReleaseManifestError("source evidence commits do not match build sources")
    if attestations != {
        "verifier": "gh attestation verify",
        "deny_self_hosted_runners": True,
        "sbom_predicate_type": source_sbom_predicate_type,
        "provenance_predicate_type": source_provenance_predicate_type,
    }:
        raise ReleaseManifestError("source evidence attestation identity is invalid")
    required_quality = ("tests", "failures", "errors", "skipped", "pytest_seconds")
    if (
        any(not isinstance(quality.get(name), (int, float)) for name in required_quality)
        or quality["tests"] < 1
        or quality["failures"] != 0
        or quality["errors"] != 0
    ):
        raise ReleaseManifestError("source evidence quality proof is invalid")
    if len(assets) != len(source_evidence_public_names):
        raise ReleaseManifestError("source evidence asset set mismatch")
    asset_names: list[str] = []
    normalized_assets: list[dict[str, Any]] = []
    checksums = load_checksum_entries(public_site_directory / public_checksum_name)
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseManifestError("source evidence has an invalid asset")
        name = asset.get("name")
        expected_hash = asset.get("sha256")
        expected_size = asset.get("size")
        if (
            not isinstance(name, str)
            or name not in source_evidence_public_names
            or name in asset_names
            or not isinstance(expected_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None
            or not isinstance(expected_size, int)
            or expected_size < 1
        ):
            raise ReleaseManifestError("source evidence has an invalid asset")
        for directory, label in ((release_directory, "release"), (public_site_directory, "public")):
            path = directory / name
            if not path.is_file() or path.stat().st_size != expected_size or sha256_file(path) != expected_hash:
                raise ReleaseManifestError(f"{label} source evidence hash mismatch: {name}")
        if checksums.get(name) != expected_hash:
            raise ReleaseManifestError(f"SHA256SUMS mismatch for {name}")
        if Path(name).suffix.lower() in {".json", ".md"}:
            assert_public_evidence_privacy(name, (public_site_directory / name).read_bytes())
        asset_names.append(name)
        normalized_assets.append({"name": name, "sha256": expected_hash, "size": expected_size})
    if set(asset_names) != set(source_evidence_public_names):
        raise ReleaseManifestError("source evidence asset set mismatch")
    return {
        "schema_version": 1,
        "source_commit": source_commit,
        "source_ref": source_ref,
        "source_commits": source_commits,
        "repository": repository,
        "signer_workflow": signer_workflow,
        "attestations": attestations,
        "quality": quality,
        "assets": sorted(normalized_assets, key=lambda asset: asset["name"]),
    }


def validate_deployment_evidence(
    evidence_path: Path,
    *,
    expected_deployment_id: str,
    expected_public_commit: str,
) -> dict[str, Any]:
    evidence = load_json_object(evidence_path)
    deployment = evidence.get("deployment")
    if evidence.get("schema_version") != 1 or not isinstance(deployment, dict):
        raise ReleaseManifestError("Cloudflare deployment evidence has an invalid schema")
    required = ("id", "project", "branch", "public_commit", "url", "status")
    if any(not isinstance(deployment.get(field), str) or not deployment[field].strip() for field in required):
        raise ReleaseManifestError("Cloudflare deployment evidence is incomplete")
    if deployment["id"] != expected_deployment_id:
        raise ReleaseManifestError("Cloudflare deployment ID does not match captured artifact deployment")
    if deployment["project"] != "codex-home-manager":
        raise ReleaseManifestError("Cloudflare deployment project is not codex-home-manager")
    if deployment["branch"] != "main":
        raise ReleaseManifestError("Cloudflare deployment branch is not main")
    if deployment["public_commit"] != expected_public_commit:
        raise ReleaseManifestError("Cloudflare deployment public commit does not match artifact commit")
    if deployment["status"] != "success":
        raise ReleaseManifestError("Cloudflare deployment did not succeed")
    parsed_url = urlparse(deployment["url"])
    if parsed_url.scheme != "https" or not parsed_url.netloc or parsed_url.path not in {"", "/"}:
        raise ReleaseManifestError("Cloudflare deployment URL is invalid")
    return {field: deployment[field] for field in required}


def validate_github_release_evidence(
    evidence_path: Path,
    public_site_directory: Path,
    source_evidence: dict[str, Any],
) -> dict[str, Any]:
    evidence = load_json_object(evidence_path)
    release = evidence.get("release")
    repository = evidence.get("repository")
    if evidence.get("schema_version") != 1 or not isinstance(release, dict) or not isinstance(repository, str):
        raise ReleaseManifestError("GitHub release evidence has an invalid schema")
    repository_parts = repository.split("/")
    if (
        len(repository_parts) != 2
        or any(not part or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in part) for part in repository_parts)
    ):
        raise ReleaseManifestError("GitHub release repository is invalid")
    release_id = release.get("id")
    tag = release.get("tag_name")
    if not isinstance(release_id, int) or release_id < 1 or not isinstance(tag, str) or not tag:
        raise ReleaseManifestError("GitHub release ID or tag is invalid")
    bundle, _ = load_public_bundle(public_site_directory)
    expected_artifacts = {
        artifact["name"]: artifact
        for artifact in bundle["artifacts"]
        if isinstance(artifact, dict) and artifact.get("kind") in {"exe", "zip"}
    }
    expected_artifacts.update({asset["name"]: asset for asset in source_evidence["assets"]})
    normalized = validate_github_release_payload(
        release,
        expected_repository=repository,
        expected_tag=tag,
        expected_release_id=release_id,
        expected_asset_names=set(expected_artifacts),
        require_draft=True,
    )
    evidence_assets = {asset["name"]: asset for asset in release["assets"]}
    artifact_records = []
    for name, expected in expected_artifacts.items():
        evidence_asset = evidence_assets[name]
        if evidence_asset.get("sha256") != expected["sha256"] or evidence_asset.get("size") != expected["size"]:
            raise ReleaseManifestError(f"GitHub draft release artifact mismatch: {name}")
        artifact_records.append({"name": name, "sha256": expected["sha256"], "size": expected["size"]})
    return {
        "release_id": release_id,
        "repository": repository,
        "tag": tag,
        "html_url": normalized["html_url"],
        "draft_verified_before_signing": True,
        "artifact_assets": sorted(artifact_records, key=lambda record: record["name"]),
        "metadata_assets": list(github_metadata_names),
    }


def load_private_key(private_key_path: Path) -> Ed25519PrivateKey:
    private_key_path = validate_private_key_path(private_key_path)
    if not private_key_path.is_file():
        raise ReleaseManifestError(f"release private key does not exist: {private_key_path}")
    try:
        key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    except (OSError, ValueError) as error:
        raise ReleaseManifestError("release private key is unreadable") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise ReleaseManifestError("release private key is not an Ed25519 key")
    return key


def create_signed_manifest(
    *,
    build_source_snapshot_path: Path,
    artifact_public_source_snapshot_path: Path,
    dist_directory: Path,
    release_directory: Path,
    public_site_directory: Path,
    artifact_deployment_evidence_path: Path,
    github_release_evidence_path: Path,
    source_evidence_proof_path: Path,
    private_key_path: Path,
    trusted_public_key_fingerprint_path: Path,
    manifest_path: Path,
    signature_path: Path,
) -> None:
    build_sources = load_build_source_snapshot(build_source_snapshot_path, verify_current=True)
    source_evidence = load_source_evidence_proof(
        source_evidence_proof_path,
        build_sources=build_sources,
        release_directory=release_directory,
        public_site_directory=public_site_directory,
    )
    artifact_public_source, expected_deployment_id = load_artifact_public_source_snapshot(artifact_public_source_snapshot_path)
    deployment = validate_deployment_evidence(
        artifact_deployment_evidence_path,
        expected_deployment_id=expected_deployment_id,
        expected_public_commit=artifact_public_source["head"],
    )
    github = validate_github_release_evidence(github_release_evidence_path, public_site_directory, source_evidence)
    if github["repository"] != source_evidence["repository"]:
        raise ReleaseManifestError("GitHub release repository does not match source evidence repository")
    trusted_fingerprint = load_trusted_public_key_fingerprint(trusted_public_key_fingerprint_path)
    private_key = load_private_key(private_key_path)
    assert_public_key_matches_trust(private_key.public_key(), trusted_fingerprint)
    validate_public_site_key_pin(public_site_directory, trusted_fingerprint)
    local_records = [record_for_path(release_directory / name) for name in sorted(local_artifact_names)]
    public_records = collect_public_artifact_records(public_site_directory, local_records, source_evidence)
    dist_records = public_dist_file_records(dist_directory)
    public_site_dist_records = verify_public_site_dist(dist_directory, public_site_directory)
    current_public_state = repository_state(
        "artifact public",
        public_site_directory.parent,
        require_clean=True,
    )
    require_main_branch("artifact public", current_public_state)
    if current_public_state["head"] != artifact_public_source["head"]:
        raise ReleaseManifestError("public HEAD changed after artifact capture")
    manifest = {
        "schema_version": 4,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sources": {"build": build_sources, "artifact_public": artifact_public_source},
        "dist_files": dist_records,
        "public_site_dist_files": public_site_dist_records,
        "local_artifacts": local_records,
        "public_artifacts": public_records,
        "source_evidence": source_evidence,
        "public_key_fingerprint": trusted_fingerprint,
        "cloudflare": {"artifact_deployment": deployment},
        "github": github,
    }
    manifest_bytes = canonical_json_bytes(manifest)
    write_bytes(manifest_path, manifest_bytes)
    write_bytes(signature_path, base64.b64encode(private_key.sign(manifest_bytes)) + b"\n")


def verify_file_records(records: Any, directory: Path, label: str, *, require_exact_set: bool = False) -> None:
    if not isinstance(records, list) or not records:
        raise ReleaseManifestError(f"manifest has no {label} records")
    expected_paths: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ReleaseManifestError(f"invalid {label} record")
        relative_path = record.get("path")
        expected_hash = record.get("sha256")
        expected_size = record.get("size")
        if (
            not isinstance(relative_path, str)
            or Path(relative_path).is_absolute()
            or ".." in Path(relative_path).parts
            or not isinstance(expected_hash, str)
            or not isinstance(expected_size, int)
        ):
            raise ReleaseManifestError(f"invalid {label} record")
        path = directory / Path(relative_path)
        if not path.is_file() or path.stat().st_size != expected_size or sha256_file(path) != expected_hash:
            raise ReleaseManifestError(f"{label} hash mismatch: {relative_path}")
        expected_paths.add(Path(relative_path).as_posix())
    if require_exact_set:
        actual_paths = {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}
        if actual_paths != expected_paths:
            raise ReleaseManifestError(f"{label} file set mismatch")


def verify_manifest_signature(manifest_bytes: bytes, signature_bytes: bytes, public_key: Ed25519PublicKey) -> None:
    try:
        signature = base64.b64decode(signature_bytes.strip(), validate=True)
        public_key.verify(signature, manifest_bytes)
    except (ValueError, InvalidSignature) as error:
        raise ReleaseManifestError("signature verification failed") from error


def verify_release(
    *,
    manifest_path: Path,
    signature_path: Path,
    public_key_path: Path,
    trusted_public_key_fingerprint_path: Path,
    root_repository: Path,
    manager_repository: Path,
    public_repository: Path,
    dist_directory: Path,
    release_directory: Path,
    public_site_directory: Path,
    artifact_deployment_evidence_path: Path,
    github_release_evidence_path: Path,
    source_evidence_proof_path: Path,
) -> None:
    manifest_bytes = manifest_path.read_bytes()
    public_key = load_public_key(public_key_path)
    trusted_fingerprint = load_trusted_public_key_fingerprint(trusted_public_key_fingerprint_path)
    assert_public_key_matches_trust(public_key, trusted_fingerprint)
    validate_public_site_key_pin(public_site_directory, trusted_fingerprint)
    try:
        verify_manifest_signature(manifest_bytes, signature_path.read_bytes(), public_key)
    except OSError as error:
        raise ReleaseManifestError("signature verification failed") from error
    manifest = load_json_object(manifest_path)
    if manifest.get("schema_version") != 4:
        raise ReleaseManifestError("unsupported release manifest schema")
    if manifest.get("public_key_fingerprint") != trusted_fingerprint:
        raise ReleaseManifestError("manifest public key fingerprint mismatch")

    sources = manifest.get("sources")
    if not isinstance(sources, dict) or not isinstance(sources.get("build"), dict):
        raise ReleaseManifestError("manifest build source state is missing")
    for name, repository in {"root": root_repository, "manager": manager_repository}.items():
        expected = valid_source_state(name, sources["build"].get(name))
        actual = repository_state(name, repository, require_clean=True)
        if actual["head"] != expected["head"]:
            raise ReleaseManifestError(f"{name} HEAD mismatch")
    source_evidence = load_source_evidence_proof(
        source_evidence_proof_path,
        build_sources=sources["build"],
        release_directory=release_directory,
        public_site_directory=public_site_directory,
    )
    if manifest.get("source_evidence") != source_evidence:
        raise ReleaseManifestError("manifest source evidence proof mismatch")
    artifact_public_source = valid_source_state("artifact public", sources.get("artifact_public"))
    require_main_branch("artifact public", artifact_public_source)
    public_state = repository_state("public", public_repository, require_clean=True)
    if public_state["branch"] != "main":
        raise ReleaseManifestError("public repository must be on main")
    try:
        run_git(public_repository, "merge-base", "--is-ancestor", artifact_public_source["head"], public_state["head"])
    except ReleaseManifestError as error:
        raise ReleaseManifestError("public artifact commit is not an ancestor of current public HEAD") from error

    cloudflare = manifest.get("cloudflare")
    if not isinstance(cloudflare, dict):
        raise ReleaseManifestError("manifest Cloudflare proof is missing")
    deployment = validate_deployment_evidence(
        artifact_deployment_evidence_path,
        expected_deployment_id=str(cloudflare.get("artifact_deployment", {}).get("id", "")),
        expected_public_commit=artifact_public_source["head"],
    )
    if cloudflare.get("artifact_deployment") != deployment:
        raise ReleaseManifestError("manifest Cloudflare artifact deployment mismatch")
    dist_records = manifest.get("dist_files")
    public_site_dist_records = manifest.get("public_site_dist_files")
    if dist_records != public_site_dist_records:
        raise ReleaseManifestError("manifest build and public site dist records differ")
    verify_file_records(dist_records, dist_directory, "dist", require_exact_set=True)
    actual_public_site_dist_records = verify_public_site_dist(dist_directory, public_site_directory)
    if public_site_dist_records != actual_public_site_dist_records:
        raise ReleaseManifestError("public site dist manifest mismatch")
    verify_file_records(manifest.get("local_artifacts"), release_directory, "local artifact")
    verify_file_records(manifest.get("public_artifacts"), public_site_directory, "public artifact")
    local_records = [record_for_path(release_directory / name) for name in sorted(local_artifact_names)]
    public_records = collect_public_artifact_records(public_site_directory, local_records, source_evidence)
    if manifest.get("public_artifacts") != public_records:
        raise ReleaseManifestError("public artifact manifest mismatch")
    github = validate_github_release_evidence(github_release_evidence_path, public_site_directory, source_evidence)
    if github["repository"] != source_evidence["repository"]:
        raise ReleaseManifestError("GitHub release repository does not match source evidence repository")
    if manifest.get("github") != github:
        raise ReleaseManifestError("manifest GitHub release proof mismatch")


def normalize_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.params or parsed.query or parsed.fragment:
        raise ReleaseManifestError("deployment URL must be an absolute HTTPS URL without a path query or fragment")
    return value.rstrip("/")


def download_bytes(url: str) -> bytes:
    try:
        parsed_url = urlparse(url)
        is_github_json_api = parsed_url.hostname == "api.github.com" and "/releases/assets/" not in parsed_url.path
        headers = {
            "Accept": "application/vnd.github+json" if is_github_json_api else "application/octet-stream",
            "User-Agent": "codex-home-manager-release-verifier",
        }
        if is_github_json_api:
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        github_token = os.environ.get("GITHUB_TOKEN")
        if github_token and parsed_url.hostname in {"api.github.com", "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}:
            headers["Authorization"] = f"Bearer {github_token}"
        request = Request(url, headers=headers)
        with urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise ReleaseManifestError(f"online release URL returned HTTP {response.status}: {url}")
            return response.read()
    except HTTPError as error:
        raise ReleaseManifestError(f"online release URL returned HTTP {error.code}: {url}") from error
    except URLError as error:
        raise ReleaseManifestError(f"cannot download online release URL: {url}: {error.reason}") from error


def validate_online_public_artifacts(records: Any, artifact_bytes: dict[str, bytes]) -> None:
    if not isinstance(records, list) or not records:
        raise ReleaseManifestError("manifest has no public artifact records")
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ReleaseManifestError("manifest has an invalid public artifact record")
        path = record["path"]
        content = artifact_bytes.get(path)
        if content is None or len(content) != record.get("size") or sha256_bytes(content) != record.get("sha256"):
            raise ReleaseManifestError(f"online public artifact hash mismatch: {path}")

    checksum_content = artifact_bytes.get(public_checksum_name)
    bundle_content = artifact_bytes.get(public_bundle_name)
    if checksum_content is None or bundle_content is None:
        raise ReleaseManifestError("online release is missing its checksum or artifact bundle")
    try:
        checksum_entries = {
            line.split("  ", maxsplit=1)[1]: line.split("  ", maxsplit=1)[0]
            for line in checksum_content.decode("utf-8").splitlines()
            if line
        }
        bundle = json.loads(bundle_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, IndexError) as error:
        raise ReleaseManifestError("online checksum or artifact bundle is invalid") from error
    if not isinstance(bundle.get("artifacts"), list):
        raise ReleaseManifestError("online artifact bundle is invalid")
    for artifact in bundle["artifacts"]:
        if not isinstance(artifact, dict):
            raise ReleaseManifestError("online artifact bundle is invalid")
        name = artifact.get("name")
        content = artifact_bytes.get(name)
        if (
            not isinstance(name, str)
            or content is None
            or artifact.get("sha256") != sha256_bytes(content)
            or artifact.get("size") != len(content)
            or checksum_entries.get(name) != sha256_bytes(content)
        ):
            raise ReleaseManifestError(f"online artifact bundle mismatch: {name}")
    if checksum_entries.get(public_bundle_name) != sha256_bytes(bundle_content):
        raise ReleaseManifestError("online checksum does not bind the artifact bundle")
    for record in records:
        if record["path"] != public_checksum_name and checksum_entries.get(record["path"]) != record["sha256"]:
            raise ReleaseManifestError(f"online checksum does not bind public artifact: {record['path']}")


def validate_online_source_evidence(value: Any, build_sources: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != 1 or not isinstance(build_sources, dict):
        raise ReleaseManifestError("online release source evidence proof is invalid")
    source_commit = value.get("source_commit")
    source_commits = value.get("source_commits")
    repository = value.get("repository")
    assets = value.get("assets")
    quality = value.get("quality")
    if (
        not isinstance(source_commit, str)
        or re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        or value.get("source_ref") != "refs/heads/source"
        or not isinstance(source_commits, dict)
        or not isinstance(repository, str)
        or not isinstance(value.get("signer_workflow"), str)
        or value.get("attestations")
        != {
            "verifier": "gh attestation verify",
            "deny_self_hosted_runners": True,
            "sbom_predicate_type": source_sbom_predicate_type,
            "provenance_predicate_type": source_provenance_predicate_type,
        }
        or not isinstance(assets, list)
        or not isinstance(quality, dict)
    ):
        raise ReleaseManifestError("online release source evidence proof is invalid")
    validate_repository_name(repository, "online source evidence repository")
    validate_signer_workflow(repository, value["signer_workflow"])
    try:
        expected_source_commits = {name: build_sources[name]["head"] for name in ("root", "manager")}
    except (KeyError, TypeError) as error:
        raise ReleaseManifestError("online release build source proof is invalid") from error
    if source_commits != expected_source_commits:
        raise ReleaseManifestError("online source evidence commits do not match build sources")
    if (
        not isinstance(quality.get("tests"), int)
        or quality["tests"] < 1
        or quality.get("failures") != 0
        or quality.get("errors") != 0
    ):
        raise ReleaseManifestError("online source evidence quality proof is invalid")
    normalized_assets: list[dict[str, Any]] = []
    names: set[str] = set()
    for asset in assets:
        if (
            not isinstance(asset, dict)
            or not isinstance(asset.get("name"), str)
            or asset["name"] not in source_evidence_public_names
            or asset["name"] in names
            or not isinstance(asset.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]) is None
            or not isinstance(asset.get("size"), int)
            or asset["size"] < 1
        ):
            raise ReleaseManifestError("online source evidence asset proof is invalid")
        names.add(asset["name"])
        normalized_assets.append(asset)
    if names != set(source_evidence_public_names):
        raise ReleaseManifestError("online source evidence asset set mismatch")
    if normalized_assets != sorted(normalized_assets, key=lambda asset: asset["name"]):
        raise ReleaseManifestError("online source evidence asset records are not canonical")
    return value


def validate_public_dist_records(records: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(records, list) or not records:
        raise ReleaseManifestError(f"manifest has no {label} records")
    validated: list[dict[str, Any]] = []
    paths: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise ReleaseManifestError(f"manifest has an invalid {label} record")
        path = record.get("path")
        sha256 = record.get("sha256")
        size = record.get("size")
        if (
            not isinstance(path, str)
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
            or not isinstance(size, int)
            or size < 0
        ):
            raise ReleaseManifestError(f"manifest has an invalid {label} record")
        validate_public_dist_relative_path(path)
        if path in paths:
            raise ReleaseManifestError(f"manifest has duplicate {label} path: {path}")
        paths.append(path)
        validated.append(record)
    if paths != sorted(paths) or "index.html" not in paths:
        raise ReleaseManifestError(f"manifest has a non-canonical {label} record set")
    return validated


def validate_online_public_site_dist(
    records: Any,
    deployed_bytes: dict[str, bytes],
    deployment_label: str,
) -> None:
    for record in validate_public_dist_records(records, "public site dist"):
        content = deployed_bytes.get(record["path"])
        if content is None or len(content) != record["size"] or sha256_bytes(content) != record["sha256"]:
            raise ReleaseManifestError(
                f"{deployment_label} public site dist hash mismatch: {record['path']}"
            )


def verify_online_release(
    *,
    metadata_base_url: str,
    expected_artifact_deployment_url: str,
    expected_artifact_deployment_id: str,
    expected_artifact_public_commit: str,
    expected_github_repository: str,
    expected_github_tag: str,
    expected_github_release_id: int,
    trusted_public_key_fingerprint_path: Path,
    fetch_bytes: Callable[[str], bytes] = download_bytes,
) -> None:
    metadata_base_url = normalize_base_url(metadata_base_url)
    expected_artifact_deployment_url = normalize_base_url(expected_artifact_deployment_url)
    manifest_url = urljoin(metadata_base_url + "/", "release-manifest.json")
    signature_url = urljoin(metadata_base_url + "/", "release-manifest.json.sig")
    public_key_url = urljoin(metadata_base_url + "/", "release-signing-public-key.pem")
    fingerprint_url = urljoin(metadata_base_url + "/", release_fingerprint_name)
    manifest_bytes = fetch_bytes(manifest_url)
    signature_bytes = fetch_bytes(signature_url)
    public_key_bytes = fetch_bytes(public_key_url)
    published_fingerprint_bytes = fetch_bytes(fingerprint_url)
    try:
        public_key = serialization.load_pem_public_key(public_key_bytes)
    except ValueError as error:
        raise ReleaseManifestError("online release public key is unreadable") from error
    if not isinstance(public_key, Ed25519PublicKey):
        raise ReleaseManifestError("online release public key is not an Ed25519 key")
    trusted_fingerprint = load_trusted_public_key_fingerprint(trusted_public_key_fingerprint_path)
    assert_public_key_matches_trust(public_key, trusted_fingerprint)
    try:
        published_fingerprint = normalize_fingerprint(published_fingerprint_bytes.decode("ascii"))
    except UnicodeDecodeError as error:
        raise ReleaseManifestError("online release fingerprint is invalid") from error
    if published_fingerprint != trusted_fingerprint:
        raise ReleaseManifestError("online release fingerprint does not match the private root pin")
    verify_manifest_signature(manifest_bytes, signature_bytes, public_key)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseManifestError("online release manifest is invalid") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 4:
        raise ReleaseManifestError("online release manifest has an unsupported schema")
    if manifest.get("public_key_fingerprint") != trusted_fingerprint:
        raise ReleaseManifestError("online release manifest public key fingerprint mismatch")
    sources = manifest.get("sources")
    if not isinstance(sources, dict):
        raise ReleaseManifestError("online release manifest has no source proof")
    source_evidence = validate_online_source_evidence(manifest.get("source_evidence"), sources.get("build"))
    cloudflare = manifest.get("cloudflare")
    if not isinstance(cloudflare, dict) or not isinstance(cloudflare.get("artifact_deployment"), dict):
        raise ReleaseManifestError("online release manifest has no artifact deployment proof")
    artifact_deployment = cloudflare["artifact_deployment"]
    if artifact_deployment.get("project") != "codex-home-manager" or artifact_deployment.get("branch") != "main":
        raise ReleaseManifestError("online release artifact deployment project or branch mismatch")
    if artifact_deployment.get("status") != "success":
        raise ReleaseManifestError("online release artifact deployment status mismatch")
    if artifact_deployment.get("id") != expected_artifact_deployment_id:
        raise ReleaseManifestError("online release artifact deployment ID mismatch")
    if artifact_deployment.get("public_commit") != expected_artifact_public_commit:
        raise ReleaseManifestError("online release artifact deployment public commit mismatch")
    artifact_deployment_url = normalize_base_url(str(artifact_deployment.get("url", "")))
    if artifact_deployment_url != expected_artifact_deployment_url:
        raise ReleaseManifestError("online release artifact deployment URL mismatch")
    dist_records = validate_public_dist_records(manifest.get("dist_files"), "dist")
    public_site_dist_records = validate_public_dist_records(
        manifest.get("public_site_dist_files"), "public site dist"
    )
    if dist_records != public_site_dist_records:
        raise ReleaseManifestError("online release build and public site dist records differ")
    artifact_deployment_dist_bytes = {
        record["path"]: fetch_bytes(urljoin(artifact_deployment_url + "/", record["path"]))
        for record in public_site_dist_records
    }
    metadata_deployment_dist_bytes = {
        record["path"]: fetch_bytes(urljoin(metadata_base_url + "/", record["path"]))
        for record in public_site_dist_records
    }
    validate_online_public_site_dist(
        public_site_dist_records,
        artifact_deployment_dist_bytes,
        "artifact deployment",
    )
    validate_online_public_site_dist(
        public_site_dist_records,
        metadata_deployment_dist_bytes,
        "metadata deployment",
    )
    github = manifest.get("github")
    if not isinstance(github, dict):
        raise ReleaseManifestError("online release manifest has no GitHub release proof")
    if source_evidence["repository"] != expected_github_repository:
        raise ReleaseManifestError("online source evidence repository does not match GitHub release")
    if (
        github.get("repository") != expected_github_repository
        or github.get("tag") != expected_github_tag
        or github.get("release_id") != expected_github_release_id
        or github.get("metadata_assets") != list(github_metadata_names)
    ):
        raise ReleaseManifestError("online release GitHub proof mismatch")
    github_artifact_records = github.get("artifact_assets")
    if not isinstance(github_artifact_records, list) or len(github_artifact_records) != 2 + len(source_evidence_public_names):
        raise ReleaseManifestError("online release GitHub artifact proof is invalid")
    github_artifact_names: set[str] = set()
    for record in github_artifact_records:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("name"), str)
            or Path(record["name"]).name != record["name"]
            or record["name"] in github_artifact_names
            or not isinstance(record.get("sha256"), str)
            or len(record["sha256"]) != 64
            or not isinstance(record.get("size"), int)
        ):
            raise ReleaseManifestError("online release GitHub artifact proof is invalid")
        github_artifact_names.add(record["name"])
    source_asset_records = {asset["name"]: asset for asset in source_evidence["assets"]}
    github_records_by_name = {record["name"]: record for record in github_artifact_records}
    for name, source_record in source_asset_records.items():
        github_record = github_records_by_name.get(name)
        if github_record != source_record:
            raise ReleaseManifestError(f"online GitHub source evidence mismatch: {name}")
    connector_names = github_artifact_names - set(source_evidence_public_names)
    if len(connector_names) != 2 or {Path(name).suffix.lower() for name in connector_names} != {".exe", ".zip"}:
        raise ReleaseManifestError("online release GitHub artifact proof must contain the connector EXE and ZIP")

    github_api_url = f"https://api.github.com/repos/{expected_github_repository}/releases/tags/{quote(expected_github_tag, safe='')}"
    try:
        github_release = json.loads(fetch_bytes(github_api_url).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseManifestError("GitHub release API payload is invalid") from error
    expected_github_asset_names = github_artifact_names | set(github_metadata_names)
    normalized_github_release = validate_github_release_payload(
        github_release,
        expected_repository=expected_github_repository,
        expected_tag=expected_github_tag,
        expected_release_id=expected_github_release_id,
        expected_asset_names=expected_github_asset_names,
        require_draft=False,
    )
    github_asset_bytes = {
        asset["name"]: fetch_bytes(asset["browser_download_url"])
        for asset in normalized_github_release["assets"]
    }
    cloudflare_metadata = {
        release_manifest_name: manifest_bytes,
        release_signature_name: signature_bytes,
        release_public_key_name: public_key_bytes,
        release_fingerprint_name: published_fingerprint_bytes,
    }
    for name, cloudflare_content in cloudflare_metadata.items():
        github_content = github_asset_bytes.get(name)
        if github_content != cloudflare_content:
            raise ReleaseManifestError(f"Cloudflare and GitHub release metadata drift: {name}")
    records = manifest.get("public_artifacts")
    if not isinstance(records, list):
        raise ReleaseManifestError("online release manifest has no public artifacts")
    artifact_bytes: dict[str, bytes] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            raise ReleaseManifestError("online release manifest has an invalid public artifact")
        path = record["path"]
        if Path(path).name != path:
            raise ReleaseManifestError("online release manifest has an invalid public artifact path")
        artifact_bytes[path] = fetch_bytes(urljoin(artifact_deployment_url + "/", path))
    validate_online_public_artifacts(records, artifact_bytes)
    for record in github_artifact_records:
        name = record["name"]
        cloudflare_content = artifact_bytes.get(name)
        github_content = github_asset_bytes.get(name)
        if (
            cloudflare_content is None
            or github_content is None
            or len(github_content) != record["size"]
            or sha256_bytes(github_content) != record["sha256"]
            or github_content != cloudflare_content
        ):
            raise ReleaseManifestError(f"Cloudflare and GitHub artifact drift: {name}")
    stable_aliases = {
        ".exe": ("codex-home-manager-local-win-x64.exe", "downloads/latest/windows-x64.exe"),
        ".zip": ("codex-home-manager-local-win-x64.zip", "downloads/latest/windows-x64.zip"),
    }
    for record in github_artifact_records:
        if record["name"] not in connector_names:
            continue
        expected_content = artifact_bytes[record["name"]]
        for alias in stable_aliases[Path(record["name"]).suffix.lower()]:
            if fetch_bytes(urljoin(metadata_base_url + "/", alias)) != expected_content:
                raise ReleaseManifestError(f"stable release alias still serves an older artifact: {alias}")


def path_argument(value: str) -> Path:
    return Path(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create and verify signed Codex Home Manager release manifests.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    keygen = subparsers.add_parser("keygen")
    keygen.add_argument("--private-key", required=True, type=path_argument)
    keygen.add_argument("--public-key", required=True, type=path_argument)
    keygen.add_argument("--trusted-public-key-fingerprint", required=True, type=path_argument)

    build_capture = subparsers.add_parser("capture-build-source")
    build_capture.add_argument("--output", required=True, type=path_argument)
    build_capture.add_argument("--root-repo", required=True, type=path_argument)
    build_capture.add_argument("--manager-repo", required=True, type=path_argument)

    public_capture = subparsers.add_parser("capture-artifact-public-source")
    public_capture.add_argument("--output", required=True, type=path_argument)
    public_capture.add_argument("--public-repo", required=True, type=path_argument)
    public_capture.add_argument("--artifact-deployment-id", required=True)

    build_validate = subparsers.add_parser("validate-build-source")
    build_validate.add_argument("--source-snapshot", required=True, type=path_argument)

    source_evidence = subparsers.add_parser("prepare-source-evidence")
    source_evidence.add_argument("--evidence-dir", required=True, type=path_argument)
    source_evidence.add_argument("--source-commit", required=True)
    source_evidence.add_argument("--repository", required=True)
    source_evidence.add_argument("--signer-workflow", required=True)
    source_evidence.add_argument("--build-source-snapshot", required=True, type=path_argument)
    source_evidence.add_argument("--release-dir", required=True, type=path_argument)
    source_evidence.add_argument("--public-site", required=True, type=path_argument)
    source_evidence.add_argument("--proof", required=True, type=path_argument)

    public_dist_plan = subparsers.add_parser("plan-public-dist-sync")
    public_dist_plan.add_argument("--dist", required=True, type=path_argument)
    public_dist_plan.add_argument("--public-site", required=True, type=path_argument)
    public_dist_plan.add_argument("--output", required=True, type=path_argument)

    public_dist_verify = subparsers.add_parser("verify-public-dist")
    public_dist_verify.add_argument("--dist", required=True, type=path_argument)
    public_dist_verify.add_argument("--public-site", required=True, type=path_argument)

    deterministic_zip = subparsers.add_parser("deterministic-zip")
    deterministic_zip.add_argument("--source", required=True, type=path_argument)
    deterministic_zip.add_argument("--output", required=True, type=path_argument)
    deterministic_zip.add_argument("--source-date-epoch", required=True, type=int)

    normalize_executable = subparsers.add_parser("normalize-pyinstaller-exe")
    normalize_executable.add_argument("--path", required=True, type=path_argument)
    normalize_executable.add_argument("--source-date-epoch", required=True, type=int)

    compare_builds = subparsers.add_parser("compare-builds")
    compare_builds.add_argument("--first-dist", required=True, type=path_argument)
    compare_builds.add_argument("--second-dist", required=True, type=path_argument)
    compare_builds.add_argument("--first-exe", required=True, type=path_argument)
    compare_builds.add_argument("--second-exe", required=True, type=path_argument)
    compare_builds.add_argument("--first-zip", required=True, type=path_argument)
    compare_builds.add_argument("--second-zip", required=True, type=path_argument)

    blackbox_executable = subparsers.add_parser("blackbox-exe")
    blackbox_executable.add_argument("--executable", required=True, type=path_argument)
    blackbox_executable.add_argument("--port", type=int)

    create = subparsers.add_parser("create")
    create.add_argument("--build-source-snapshot", required=True, type=path_argument)
    create.add_argument("--artifact-public-source-snapshot", required=True, type=path_argument)
    create.add_argument("--dist", required=True, type=path_argument)
    create.add_argument("--release-dir", required=True, type=path_argument)
    create.add_argument("--public-site", required=True, type=path_argument)
    create.add_argument("--artifact-deployment-evidence", required=True, type=path_argument)
    create.add_argument("--github-release-evidence", required=True, type=path_argument)
    create.add_argument("--source-evidence-proof", required=True, type=path_argument)
    create.add_argument("--private-key", required=True, type=path_argument)
    create.add_argument("--trusted-public-key-fingerprint", required=True, type=path_argument)
    create.add_argument("--manifest", required=True, type=path_argument)
    create.add_argument("--signature", required=True, type=path_argument)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", required=True, type=path_argument)
    verify.add_argument("--signature", required=True, type=path_argument)
    verify.add_argument("--public-key", required=True, type=path_argument)
    verify.add_argument("--trusted-public-key-fingerprint", required=True, type=path_argument)
    verify.add_argument("--root-repo", required=True, type=path_argument)
    verify.add_argument("--manager-repo", required=True, type=path_argument)
    verify.add_argument("--public-repo", required=True, type=path_argument)
    verify.add_argument("--dist", required=True, type=path_argument)
    verify.add_argument("--release-dir", required=True, type=path_argument)
    verify.add_argument("--public-site", required=True, type=path_argument)
    verify.add_argument("--artifact-deployment-evidence", required=True, type=path_argument)
    verify.add_argument("--github-release-evidence", required=True, type=path_argument)
    verify.add_argument("--source-evidence-proof", required=True, type=path_argument)

    online_verify = subparsers.add_parser("online-verify")
    online_verify.add_argument("--metadata-base-url", required=True)
    online_verify.add_argument("--artifact-deployment-url", required=True)
    online_verify.add_argument("--artifact-deployment-id", required=True)
    online_verify.add_argument("--artifact-public-commit", required=True)
    online_verify.add_argument("--github-repository", required=True)
    online_verify.add_argument("--github-tag", required=True)
    online_verify.add_argument("--github-release-id", required=True, type=int)
    online_verify.add_argument("--trusted-public-key-fingerprint", required=True, type=path_argument)
    return parser


def main(arguments: list[str] | None = None) -> int:
    parsed = build_parser().parse_args(arguments)
    try:
        if parsed.command == "keygen":
            generate_key_pair(parsed.private_key, parsed.public_key, parsed.trusted_public_key_fingerprint)
        elif parsed.command == "capture-build-source":
            capture_build_source_state(parsed.output, root_repository=parsed.root_repo, manager_repository=parsed.manager_repo)
        elif parsed.command == "capture-artifact-public-source":
            capture_artifact_public_source_state(
                parsed.output,
                public_repository=parsed.public_repo,
                artifact_deployment_id=parsed.artifact_deployment_id,
            )
        elif parsed.command == "validate-build-source":
            validate_build_source_state(parsed.source_snapshot)
        elif parsed.command == "prepare-source-evidence":
            prepare_source_release_evidence(
                evidence_directory=parsed.evidence_dir,
                expected_source_commit=parsed.source_commit,
                expected_build_sources=load_build_source_snapshot(parsed.build_source_snapshot, verify_current=True),
                repository=parsed.repository,
                signer_workflow=parsed.signer_workflow,
                release_directory=parsed.release_dir,
                public_site_directory=parsed.public_site,
                proof_path=parsed.proof,
            )
        elif parsed.command == "plan-public-dist-sync":
            write_bytes(
                parsed.output,
                canonical_json_bytes(plan_public_site_dist_sync(parsed.dist, parsed.public_site)),
            )
        elif parsed.command == "verify-public-dist":
            verify_public_site_dist(parsed.dist, parsed.public_site)
        elif parsed.command == "deterministic-zip":
            create_deterministic_zip(parsed.source, parsed.output, source_date_epoch=parsed.source_date_epoch)
        elif parsed.command == "normalize-pyinstaller-exe":
            normalize_pyinstaller_executable(parsed.path, source_date_epoch=parsed.source_date_epoch)
        elif parsed.command == "compare-builds":
            compare_reproducible_builds(
                parsed.first_dist,
                parsed.second_dist,
                parsed.first_exe,
                parsed.second_exe,
                parsed.first_zip,
                parsed.second_zip,
            )
        elif parsed.command == "blackbox-exe":
            print(json.dumps(blackbox_test_executable(parsed.executable, port=parsed.port), sort_keys=True))
        elif parsed.command == "create":
            create_signed_manifest(
                build_source_snapshot_path=parsed.build_source_snapshot,
                artifact_public_source_snapshot_path=parsed.artifact_public_source_snapshot,
                dist_directory=parsed.dist,
                release_directory=parsed.release_dir,
                public_site_directory=parsed.public_site,
                artifact_deployment_evidence_path=parsed.artifact_deployment_evidence,
                github_release_evidence_path=parsed.github_release_evidence,
                source_evidence_proof_path=parsed.source_evidence_proof,
                private_key_path=parsed.private_key,
                trusted_public_key_fingerprint_path=parsed.trusted_public_key_fingerprint,
                manifest_path=parsed.manifest,
                signature_path=parsed.signature,
            )
        elif parsed.command == "verify":
            verify_release(
                manifest_path=parsed.manifest,
                signature_path=parsed.signature,
                public_key_path=parsed.public_key,
                trusted_public_key_fingerprint_path=parsed.trusted_public_key_fingerprint,
                root_repository=parsed.root_repo,
                manager_repository=parsed.manager_repo,
                public_repository=parsed.public_repo,
                dist_directory=parsed.dist,
                release_directory=parsed.release_dir,
                public_site_directory=parsed.public_site,
                artifact_deployment_evidence_path=parsed.artifact_deployment_evidence,
                github_release_evidence_path=parsed.github_release_evidence,
                source_evidence_proof_path=parsed.source_evidence_proof,
            )
        elif parsed.command == "online-verify":
            verify_online_release(
                metadata_base_url=parsed.metadata_base_url,
                expected_artifact_deployment_url=parsed.artifact_deployment_url,
                expected_artifact_deployment_id=parsed.artifact_deployment_id,
                expected_artifact_public_commit=parsed.artifact_public_commit,
                expected_github_repository=parsed.github_repository,
                expected_github_tag=parsed.github_tag,
                expected_github_release_id=parsed.github_release_id,
                trusted_public_key_fingerprint_path=parsed.trusted_public_key_fingerprint,
            )
    except ReleaseManifestError as error:
        print(f"release manifest error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
