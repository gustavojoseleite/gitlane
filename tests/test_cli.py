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
    assert args.path == "." and args.port is None and not args.no_browser
