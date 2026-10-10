from __future__ import annotations

from pathlib import Path

from backend import diagnostics


def test_current_desktop_install_for_pagination_prefers_current_appx(
    monkeypatch,
) -> None:
    current_path = Path(r"C:\Program Files\WindowsApps\OpenAI.Codex_26.831.2377.0_x64__current")

    monkeypatch.setattr(
        diagnostics,
        "inspect_current_codex_appx_install",
        lambda: {
            "available": True,
            "installPath": str(current_path),
            "version": "26.831.2377.0",
            "error": "",
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "detect_current_codex_versions",
        lambda _paths: {
            "desktopInstall": {
                "path": r"C:\Program Files\WindowsApps\OpenAI.Codex_26.803.10989.0_x64__stale",
                "version": "26.803.10989.0",
            }
        },
    )

    result = diagnostics.current_desktop_install_for_pagination(object())

    assert result == {"path": str(current_path), "version": "26.831.2377.0"}


def test_current_desktop_install_for_pagination_falls_back_when_appx_is_unavailable(
    monkeypatch,
) -> None:
    fallback = {
        "path": r"C:\Program Files\WindowsApps\OpenAI.Codex_26.803.10989.0_x64__fallback",
        "version": "26.803.10989.0",
    }
    monkeypatch.setattr(
        diagnostics,
        "inspect_current_codex_appx_install",
        lambda: {"available": False, "installPath": "", "version": "", "error": "unavailable"},
    )
    monkeypatch.setattr(
        diagnostics,
        "detect_current_codex_versions",
        lambda _paths: {"desktopInstall": fallback},
    )

    assert diagnostics.current_desktop_install_for_pagination(object()) == fallback
