from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend import diagnostics as diagnostics_module
from backend import server


def setup_function() -> None:
    server.clear_diagnostics_cache()


def test_diagnostics_cache_reuses_result_and_force_refresh_bypasses(monkeypatch) -> None:
    call_count = 0
    runtime_cache_clear_count = 0

    def fake_run_codex_diagnostics(**_kwargs):
        nonlocal call_count
        call_count += 1
        return {"generation": call_count}

    def fake_clear_diagnostics_runtime_caches() -> None:
        nonlocal runtime_cache_clear_count
        runtime_cache_clear_count += 1

    monkeypatch.setattr(server, "run_codex_diagnostics", fake_run_codex_diagnostics)
    monkeypatch.setattr(server, "clear_diagnostics_runtime_caches", fake_clear_diagnostics_runtime_caches)

    first = server.cached_codex_diagnostics("D:/Codex", 50, "zh")
    cached = server.cached_codex_diagnostics("D:/Codex", 50, "zh")
    refreshed = server.cached_codex_diagnostics("D:/Codex", 50, "zh", force_refresh=True)

    assert first == cached == {"generation": 1}
    assert refreshed == {"generation": 2}
    assert call_count == 2
    assert runtime_cache_clear_count == 1


def test_diagnostics_cache_coalesces_concurrent_scans(monkeypatch) -> None:
    scan_started = threading.Event()
    release_scan = threading.Event()
    call_count = 0

    def fake_run_codex_diagnostics(**_kwargs):
        nonlocal call_count
        call_count += 1
        scan_started.set()
        assert release_scan.wait(timeout=5)
        return {"generation": call_count}

    monkeypatch.setattr(server, "run_codex_diagnostics", fake_run_codex_diagnostics)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(server.cached_codex_diagnostics, "D:/Codex", 50, "zh")
        assert scan_started.wait(timeout=5)
        second = executor.submit(server.cached_codex_diagnostics, "D:/Codex", 50, "zh")
        release_scan.set()

    assert first.result() == second.result() == {"generation": 1}
    assert call_count == 1


def test_diagnostics_cache_coalesces_concurrent_force_refreshes(monkeypatch) -> None:
    scan_started = threading.Event()
    release_scan = threading.Event()
    call_lock = threading.Lock()
    call_count = 0
    runtime_cache_clear_count = 0

    def fake_run_codex_diagnostics(**_kwargs):
        nonlocal call_count
        with call_lock:
            call_count += 1
            generation = call_count
        scan_started.set()
        assert release_scan.wait(timeout=5)
        return {"generation": generation}

    def fake_clear_diagnostics_runtime_caches() -> None:
        nonlocal runtime_cache_clear_count
        runtime_cache_clear_count += 1

    monkeypatch.setattr(server, "run_codex_diagnostics", fake_run_codex_diagnostics)
    monkeypatch.setattr(server, "clear_diagnostics_runtime_caches", fake_clear_diagnostics_runtime_caches)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            server.cached_codex_diagnostics,
            "D:/Codex",
            50,
            "zh",
            force_refresh=True,
        )
        assert scan_started.wait(timeout=5)
        second = executor.submit(
            server.cached_codex_diagnostics,
            "D:/Codex",
            50,
            "zh",
            force_refresh=True,
        )
        time.sleep(0.05)
        assert call_count == 1
        release_scan.set()

    assert first.result() == second.result() == {"generation": 1}
    assert runtime_cache_clear_count == 1


def test_force_refresh_starts_new_generation_and_old_result_cannot_replace_it(monkeypatch) -> None:
    first_scan_started = threading.Event()
    second_scan_started = threading.Event()
    release_first_scan = threading.Event()
    release_second_scan = threading.Event()
    call_lock = threading.Lock()
    call_count = 0

    def fake_run_codex_diagnostics(**_kwargs):
        nonlocal call_count
        with call_lock:
            call_count += 1
            generation = call_count
        if generation == 1:
            first_scan_started.set()
            assert release_first_scan.wait(timeout=5)
        else:
            second_scan_started.set()
            assert release_second_scan.wait(timeout=5)
        return {"generation": generation}

    monkeypatch.setattr(server, "run_codex_diagnostics", fake_run_codex_diagnostics)

    with ThreadPoolExecutor(max_workers=2) as executor:
        old_generation = executor.submit(server.cached_codex_diagnostics, "D:/Codex", 50, "zh")
        assert first_scan_started.wait(timeout=5)

        refreshed_generation = executor.submit(
            server.cached_codex_diagnostics,
            "D:/Codex",
            50,
            "zh",
            force_refresh=True,
        )
        assert second_scan_started.wait(timeout=5)
        release_second_scan.set()
        assert refreshed_generation.result(timeout=5) == {"generation": 2}
        assert server.cached_codex_diagnostics("D:/Codex", 50, "zh") == {"generation": 2}

        release_first_scan.set()
        assert old_generation.result(timeout=5) == {"generation": 1}

    assert server.cached_codex_diagnostics("D:/Codex", 50, "zh") == {"generation": 2}
    assert call_count == 2


def test_runtime_epoch_change_starts_new_generation_and_old_result_cannot_replace_it(monkeypatch) -> None:
    first_scan_started = threading.Event()
    second_scan_started = threading.Event()
    release_first_scan = threading.Event()
    release_second_scan = threading.Event()
    call_lock = threading.Lock()
    call_count = 0

    def fake_run_codex_diagnostics(**_kwargs):
        nonlocal call_count
        with call_lock:
            call_count += 1
            generation = call_count
        if generation == 1:
            first_scan_started.set()
            assert release_first_scan.wait(timeout=5)
        else:
            second_scan_started.set()
            assert release_second_scan.wait(timeout=5)
        return {"generation": generation}

    monkeypatch.setattr(server, "run_codex_diagnostics", fake_run_codex_diagnostics)

    with ThreadPoolExecutor(max_workers=2) as executor:
        old_generation = executor.submit(server.cached_codex_diagnostics, "D:/Codex", 50, "zh")
        assert first_scan_started.wait(timeout=5)

        diagnostics_module.clear_diagnostics_runtime_caches()
        new_generation = executor.submit(server.cached_codex_diagnostics, "D:/Codex", 50, "zh")
        assert second_scan_started.wait(timeout=5)
        release_second_scan.set()
        assert new_generation.result(timeout=5) == {"generation": 2}

        release_first_scan.set()
        assert old_generation.result(timeout=5) == {"generation": 1}

    assert server.cached_codex_diagnostics("D:/Codex", 50, "zh") == {"generation": 2}
    assert call_count == 2


def test_cache_epoch_change_starts_new_generation_and_old_result_cannot_replace_it(monkeypatch) -> None:
    first_scan_started = threading.Event()
    second_scan_started = threading.Event()
    release_first_scan = threading.Event()
    release_second_scan = threading.Event()
    call_lock = threading.Lock()
    call_count = 0

    def fake_run_codex_diagnostics(**_kwargs):
        nonlocal call_count
        with call_lock:
            call_count += 1
            generation = call_count
        if generation == 1:
            first_scan_started.set()
            assert release_first_scan.wait(timeout=5)
        else:
            second_scan_started.set()
            assert release_second_scan.wait(timeout=5)
        return {"generation": generation}

    monkeypatch.setattr(server, "run_codex_diagnostics", fake_run_codex_diagnostics)

    with ThreadPoolExecutor(max_workers=2) as executor:
        old_generation = executor.submit(server.cached_codex_diagnostics, "D:/Codex", 50, "zh")
        assert first_scan_started.wait(timeout=5)

        server.clear_diagnostics_cache()
        new_generation = executor.submit(server.cached_codex_diagnostics, "D:/Codex", 50, "zh")
        assert second_scan_started.wait(timeout=5)
        release_second_scan.set()
        assert new_generation.result(timeout=5) == {"generation": 2}

        release_first_scan.set()
        assert old_generation.result(timeout=5) == {"generation": 1}

    assert server.cached_codex_diagnostics("D:/Codex", 50, "zh") == {"generation": 2}
    assert call_count == 2


def test_all_waiters_have_bounded_timeout_without_cancelling_shared_task(monkeypatch) -> None:
    scan_started = threading.Event()
    release_scan = threading.Event()

    def fake_run_codex_diagnostics(**_kwargs):
        scan_started.set()
        assert release_scan.wait(timeout=5)
        return {"generation": 1}

    monkeypatch.setattr(server, "run_codex_diagnostics", fake_run_codex_diagnostics)
    monkeypatch.setattr(server, "diagnostics_wait_timeout_seconds", 0.05)

    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = executor.submit(server.cached_codex_diagnostics, "D:/Codex", 50, "zh")
        assert scan_started.wait(timeout=5)

        started_at = time.monotonic()
        with pytest.raises(TimeoutError, match=r"diagnostics generation \d+"):
            server.cached_codex_diagnostics("D:/Codex", 50, "zh")
        elapsed_seconds = time.monotonic() - started_at

        assert elapsed_seconds < 1
        with pytest.raises(TimeoutError, match=r"diagnostics generation \d+"):
            owner.result(timeout=1)
        release_scan.set()

    monkeypatch.setattr(server, "diagnostics_wait_timeout_seconds", 1.0)
    assert server.cached_codex_diagnostics("D:/Codex", 50, "zh") == {"generation": 1}


def test_request_that_starts_scan_can_timeout_without_cancelling_shared_task(monkeypatch) -> None:
    scan_started = threading.Event()
    release_scan = threading.Event()
    call_count = 0

    def fake_run_codex_diagnostics(**_kwargs):
        nonlocal call_count
        call_count += 1
        scan_started.set()
        assert release_scan.wait(timeout=5)
        return {"generation": call_count}

    monkeypatch.setattr(server, "run_codex_diagnostics", fake_run_codex_diagnostics)
    monkeypatch.setattr(server, "diagnostics_wait_timeout_seconds", 0.05)

    release_timer = threading.Timer(0.2, release_scan.set)
    release_timer.start()
    try:
        with pytest.raises(TimeoutError, match=r"diagnostics generation \d+"):
            server.cached_codex_diagnostics("D:/Codex", 50, "zh")
        assert scan_started.is_set()
        assert call_count == 1

        monkeypatch.setattr(server, "diagnostics_wait_timeout_seconds", 1.0)
        assert server.cached_codex_diagnostics("D:/Codex", 50, "zh") == {"generation": 1}
    finally:
        release_timer.join(timeout=5)

    assert server.cached_codex_diagnostics("D:/Codex", 50, "zh") == {"generation": 1}
    assert call_count == 1


def test_cancelled_request_does_not_cancel_shared_task(monkeypatch) -> None:
    scan_started = threading.Event()
    release_scan = threading.Event()
    call_count = 0

    def fake_run_codex_diagnostics(**_kwargs):
        nonlocal call_count
        call_count += 1
        scan_started.set()
        assert release_scan.wait(timeout=5)
        return {"generation": call_count}

    monkeypatch.setattr(server, "run_codex_diagnostics", fake_run_codex_diagnostics)

    async def cancel_one_waiter_and_join_from_another() -> dict[str, int]:
        cancelled_waiter = asyncio.create_task(
            asyncio.to_thread(server.cached_codex_diagnostics, "D:/Codex", 50, "zh")
        )
        assert await asyncio.to_thread(scan_started.wait, 5)
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter

        surviving_waiter = asyncio.create_task(
            asyncio.to_thread(server.cached_codex_diagnostics, "D:/Codex", 50, "zh")
        )
        await asyncio.sleep(0.05)
        assert call_count == 1
        release_scan.set()
        return await surviving_waiter

    assert asyncio.run(cancel_one_waiter_and_join_from_another()) == {"generation": 1}
    assert call_count == 1


def test_scan_exception_reaches_waiter_and_next_generation_can_retry(monkeypatch) -> None:
    scan_started = threading.Event()
    release_scan = threading.Event()
    call_count = 0

    def fake_run_codex_diagnostics(**_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            scan_started.set()
            assert release_scan.wait(timeout=5)
            raise RuntimeError("diagnostics failed")
        return {"generation": call_count}

    monkeypatch.setattr(server, "run_codex_diagnostics", fake_run_codex_diagnostics)

    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = executor.submit(server.cached_codex_diagnostics, "D:/Codex", 50, "zh")
        assert scan_started.wait(timeout=5)
        release_timer = threading.Timer(0.05, release_scan.set)
        release_timer.start()
        try:
            with pytest.raises(RuntimeError, match="diagnostics failed"):
                server.cached_codex_diagnostics("D:/Codex", 50, "zh")
        finally:
            release_timer.join(timeout=5)
        with pytest.raises(RuntimeError, match="diagnostics failed"):
            owner.result(timeout=5)

    assert server.cached_codex_diagnostics("D:/Codex", 50, "zh") == {"generation": 2}
    assert call_count == 2


def test_clear_diagnostics_cache_clears_all_runtime_caches() -> None:
    with diagnostics_module.diagnostics_runtime_cache_lock:
        cache_epoch = diagnostics_module.diagnostics_runtime_cache_epoch
        diagnostics_module.mcp_process_snapshot_cache = (cache_epoch, time.monotonic(), {"cached": True})
        diagnostics_module.curated_plugin_registry_cache[("home", "cli")] = (
            cache_epoch,
            time.monotonic(),
            {"cached": True},
        )
        diagnostics_module.current_codex_appx_install_cache = (
            cache_epoch,
            time.monotonic(),
            {"cached": True},
        )

    server.clear_diagnostics_cache()

    with diagnostics_module.diagnostics_runtime_cache_lock:
        assert diagnostics_module.mcp_process_snapshot_cache is None
        assert diagnostics_module.curated_plugin_registry_cache == {}
        assert diagnostics_module.current_codex_appx_install_cache is None


def test_runtime_cache_clear_prevents_slow_old_probe_from_repopulating(monkeypatch) -> None:
    if diagnostics_module.os.name != "nt":
        pytest.skip("MCP process snapshot cache is Windows-specific")

    probe_started = threading.Event()
    release_probe = threading.Event()

    def fake_list_windows_processes():
        probe_started.set()
        assert release_probe.wait(timeout=5)
        return []

    diagnostics_module.clear_diagnostics_runtime_caches()
    monkeypatch.setattr(diagnostics_module, "list_windows_processes", fake_list_windows_processes)

    with ThreadPoolExecutor(max_workers=1) as executor:
        old_probe = executor.submit(diagnostics_module.scan_mcp_process_snapshot)
        assert probe_started.wait(timeout=5)
        diagnostics_module.clear_diagnostics_runtime_caches()
        release_probe.set()
        assert old_probe.result(timeout=5)["available"] is True

    with diagnostics_module.diagnostics_runtime_cache_lock:
        assert diagnostics_module.mcp_process_snapshot_cache is None
