from unittest.mock import Mock

import pytest


def test_lone_slash_does_not_crash_repl(chat, monkeypatch, capsys):
    lines = iter(["/", "/help", "/quit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(lines))
    chat.repl()
    assert "Type /help" in capsys.readouterr().out


@pytest.mark.parametrize("address", ["z" * 32, "a" * 31, "a" * 64, ""])
def test_to_does_not_select_invalid_address(chat, address):
    chat.cmd_to(address)
    assert chat.active_peer is None


def test_all_digit_address_is_not_an_index(chat):
    chat.cmd_to("1" * 32)
    assert chat.active_peer == "1" * 32


def test_peer_indices_reference_the_last_list(chat):
    chat.peers.update({"a" * 32: "Alice", "b" * 32: "Bob"})
    chat.cmd_peers()
    chat.peers = {"c" * 32: "Carol", **chat.peers}
    chat.cmd_to("2")
    assert chat.active_peer == "b" * 32


def test_cli_reports_correct_address_length(chat, capsys):
    with pytest.raises(SystemExit) as exc:
        chat.parse_args(["--to", "z" * 32])
    assert exc.value.code == 2
    assert "32 hexadecimal" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["--announce-interval", "30"], ["--announce-interval", "-1"],
    ["--path-timeout", "0"], ["--max-attachment-kib", "-1"],
    ["--attachment-quota-mib", "0"], ["--propagation-node", "bad"],
])
def test_invalid_cli_options_fail_before_network_startup(chat, argv):
    with pytest.raises(SystemExit):
        chat.parse_args(argv)


def test_cli_allows_startup_only_announces(chat):
    assert chat.parse_args(["--announce-interval", "0"]).announce_interval == 0


def test_no_recipient_draft_is_not_sent(chat, monkeypatch):
    lines = iter(["private draft", "/quit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(lines))
    send = Mock()
    monkeypatch.setattr(chat, "send_message", send)
    chat.repl()
    send.assert_not_called()
