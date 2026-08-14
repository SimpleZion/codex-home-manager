from pathlib import Path


def source_between(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_hosted_console_opens_local_full_mode_instead_of_requesting_write_token() -> None:
    main_source = (Path(__file__).resolve().parents[1] / "src" / "main.tsx").read_text(encoding="utf-8")

    connector_guard = source_between(
        main_source,
        "function requireLocalConnector(actionLabel: string): boolean",
        "async function fetchAuthorizedJson",
    )
    token_refresh = source_between(
        main_source,
        "const refreshApiToken = React.useCallback",
        "const checkLocalApiAccess = React.useCallback",
    )

    assert "if (isHostedConsole())" in connector_guard
    assert "window.open(defaultLocalApiBaseUrl" in connector_guard
    assert "http://127.0.0.1:8765" in connector_guard
    assert "if (isHostedConsole())" in token_refresh
    assert '"interactive-write"' not in token_refresh
