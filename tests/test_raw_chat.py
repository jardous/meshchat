from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import RNS


def make_link(key, *, initiator=False, identified=True):
    identity = SimpleNamespace(hash=b"\xcc" * 16) if identified else None
    return SimpleNamespace(link_id=bytes.fromhex(key), teardown_reason="remote disconnect",
                           get_remote_identity=lambda: identity, initiator=initiator,
                           destination=SimpleNamespace(identity=SimpleNamespace(hash=b"\xdd" * 16)),
                           status=RNS.Link.ACTIVE, mdu=431, teardown=Mock())


def test_remote_disconnect_only_removes_its_session(raw):
    first = make_link("a" * 32)
    second = make_link("b" * 32)
    raw._links.update({"a" * 32: first, "b" * 32: second})
    raw._active_link = "a" * 32
    raw.link_closed(first)
    assert raw._links == {"b" * 32: second}
    assert raw._active_link is None
    assert not raw._stop_event.is_set()


def test_disconnect_does_not_change_another_active_session(raw):
    first = make_link("a" * 32)
    second = make_link("b" * 32)
    raw._links.update({"a" * 32: first, "b" * 32: second})
    raw._active_link = "b" * 32
    raw.link_closed(first)
    assert raw._active_link == "b" * 32


def test_raw_packet_terminal_controls_are_escaped(raw, capsys):
    raw.packet_received(b"hello\x1b]52;c;bad\x07", SimpleNamespace(link=make_link("a" * 32)))
    output = capsys.readouterr().out
    assert "\x1b" not in output and "\\x1b" in output


def test_server_rejects_messages_before_peer_identifies(raw, capsys):
    raw.packet_received(b"secret", SimpleNamespace(link=make_link("a" * 32, identified=False)))
    output = capsys.readouterr().out
    assert "Ignoring" in output and "secret" not in output


def test_client_uses_destination_identity_for_responder(raw):
    link = make_link("a" * 32, initiator=True, identified=False)
    assert RNS.prettyhexrep(link.destination.identity.hash) == raw._peer_label(link)


def test_unidentified_peer_cannot_be_selected(raw):
    raw._links["a" * 32] = make_link("a" * 32, identified=False)
    with pytest.raises(ValueError, match="identify"):
        raw._select_peer("1")


def test_raw_repl_uses_one_reader_and_survives_invalid_commands(raw, monkeypatch):
    lines = iter(["/", "/to nope", "/quit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(lines))
    raw.input_loop()
    assert not raw._stop_event.is_set()


def test_oversize_raw_messages_are_not_sent(raw, monkeypatch, capsys):
    link = make_link("a" * 32)
    raw._links["a" * 32] = link
    lines = iter(["x" * 432, "/quit"])
    monkeypatch.setattr("builtins.input", lambda *_: next(lines))
    packet = Mock()
    monkeypatch.setattr(RNS, "Packet", packet)
    raw.input_loop(link)
    packet.assert_not_called()
    assert "Message too long" in capsys.readouterr().out


def test_invalid_client_hash_fails_before_loading_identity(raw, monkeypatch):
    load = Mock()
    monkeypatch.setattr(raw, "load_identity", load)
    with pytest.raises(ValueError):
        raw.run_client("z" * 32)
    load.assert_not_called()
