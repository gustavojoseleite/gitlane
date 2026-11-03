from __future__ import annotations

import socket

from gitlane import cli


def test_not_a_repo_exits_with_message(tmp_path, capsys):
    assert cli.main([str(tmp_path), "--no-browser"]) == 1
    err = capsys.readouterr().err
    assert "Not a Git repository" in err


def test_find_free_port_is_bindable_on_localhost():
    port = cli.find_free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_host_is_localhost_only():
    assert cli.HOST == "127.0.0.1"


def test_defaults():
    args = cli.parse_args([])
    assert args.path is None and args.port is None and not args.no_browser


def _capture_start(monkeypatch):
    """Run main() without serving: record which repo it would open."""
    import uvicorn

    import gitlane.server

    seen = {}
    real = gitlane.server.create_app

    def fake_create_app(repo_root, token, port, *a, **kw):
        seen["repo"] = repo_root
        return real(repo_root, token, port, *a, **kw)

    monkeypatch.setattr(gitlane.server, "create_app", fake_create_app)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self: None)
    return seen


def test_no_path_outside_repo_starts_with_picker(tmp_path, monkeypatch):
    seen = _capture_start(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--no-browser"]) == 0
    assert seen["repo"] is None


def test_no_path_inside_repo_opens_it(make_repo, monkeypatch):
    path = make_repo()
    (path / "sub").mkdir()
    seen = _capture_start(monkeypatch)
    monkeypatch.chdir(path / "sub")
    assert cli.main(["--no-browser"]) == 0
    assert seen["repo"] == path.resolve()
