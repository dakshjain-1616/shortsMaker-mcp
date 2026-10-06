import tomllib
from pathlib import Path

import pytest

from shortsmaker_mcp import cli


def test_cli_serves_the_app_on_the_configured_host_and_port(monkeypatch):
    calls = {}
    monkeypatch.setenv("MCP_BACKEND_API_URL", "http://backend.test")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PORT", "9123")
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.update(app=app, **kw))
    monkeypatch.setattr("sys.argv", ["shortsmaker-mcp"])

    cli.main()

    assert calls == {"app": "shortsmaker_mcp.main:app", "host": "0.0.0.0", "port": 9123}


def test_cli_help_exits_without_starting_a_server(monkeypatch, capsys):
    started = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: started.append(1))
    monkeypatch.setattr("sys.argv", ["shortsmaker-mcp", "--help"])

    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 0 and not started
    assert "MCP_BACKEND_API_URL" in capsys.readouterr().out


def test_server_version_matches_the_package_version(monkeypatch):
    monkeypatch.setenv("MCP_BACKEND_API_URL", "http://backend.test")
    from shortsmaker_mcp.main import server

    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())

    assert server.version == pyproject["project"]["version"]
