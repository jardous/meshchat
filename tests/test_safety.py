import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import LXMF
import pytest
import RNS
import RNS.vendor.umsgpack as msgpack

from chat_utils import load_identity, normalize_address, safe_text


@pytest.mark.parametrize("value", ["a" * 32, "1" * 32, "AA:" * 15 + "AA"])
def test_address_normalization(value):
    assert len(bytes.fromhex(normalize_address(value))) == 16


@pytest.mark.parametrize("value", ["z" * 32, "a" * 31, "a" * 64, "", "a " * 16, "１" * 32])
def test_invalid_addresses_rejected(value):
    with pytest.raises(ValueError):
        normalize_address(value)


def test_terminal_controls_are_inert():
    rendered = safe_text("Alice\x1b]52;c;clipboard\x07\r\b\x9b2J\u202e\nBob")
    assert all(char.isprintable() for char in rendered)
    assert "\\x1b" in rendered and "\\u202e" in rendered
    assert safe_text("hello\nworld", multiline=True) == "hello\nworld"


def test_identity_is_private_and_persistent(tmp_path):
    path = tmp_path / "identity"
    first = load_identity(path)
    second = load_identity(path)
    assert first.hash == second.hash
    assert path.stat().st_mode & 0o777 == 0o600


def test_corrupt_identity_is_not_replaced(tmp_path):
    path = tmp_path / "identity"
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="Cannot load identity"):
        load_identity(path)
    assert path.read_bytes() == b"corrupt"


def test_attachments_are_unique_and_private(chat):
    first = Path(chat._save_attachment("a" * 32, "image.png", b"first"))
    second = Path(chat._save_attachment("a" * 32, "image.png", b"second"))
    assert first != second
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"
    assert first.stat().st_mode & 0o777 == 0o600
    assert first.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("filename", ["../secret", "/tmp/secret", "a\\b", "x\x00.png", ".", "..", 123])
def test_unsafe_attachment_names_are_rejected(chat, filename):
    with pytest.raises(ValueError):
        chat._save_attachment("a" * 32, filename, b"data")


@pytest.mark.parametrize("payload", [123, "not bytes", [1, 2]])
def test_attachment_payload_must_be_bytes(chat, payload):
    with pytest.raises(ValueError, match="bytes"):
        chat._save_attachment("a" * 32, "file", payload)


def test_attachment_size_and_total_quota(chat, monkeypatch):
    monkeypatch.setattr(chat, "_max_attachment_bytes", 4)
    monkeypatch.setattr(chat, "_attachment_quota_bytes", 4096)
    with pytest.raises(ValueError, match="per-file"):
        chat._save_attachment("a" * 32, "large", b"12345")
    chat._save_attachment("a" * 32, "small", b"1234")
    with pytest.raises(ValueError, match="quota"):
        chat._save_attachment("a" * 32, "empty", b"")


def test_open_never_uses_a_shell(chat, monkeypatch):
    dangerous_path = "/somewhere/photo'$(printf BAD).png"
    chat._images.append(dangerous_path)
    popen = Mock()
    monkeypatch.setattr(chat.subprocess, "Popen", popen)
    chat.cmd_open("1")
    popen.assert_called_once_with(["feh", "--", dangerous_path])


def test_missing_image_viewer_is_reported(chat, monkeypatch, capsys):
    chat._images.append("image.png")
    monkeypatch.setattr(chat.subprocess, "Popen", Mock(side_effect=FileNotFoundError("feh not installed")))
    chat.cmd_open("1")
    assert "feh not installed" in capsys.readouterr().out


@pytest.mark.parametrize("reason", [LXMF.LXMessage.SOURCE_UNKNOWN, LXMF.LXMessage.SIGNATURE_INVALID])
def test_unverified_messages_cannot_display_content_save_files_or_select_peer(chat, monkeypatch, reason):
    receive = Mock()
    save = Mock()
    monkeypatch.setattr(chat, "recv", receive)
    monkeypatch.setattr(chat, "_save_attachment", save)
    message = SimpleNamespace(signature_validated=False, unverified_reason=reason,
                              content=b"do not display", source_hash=b"\xaa" * 16,
                              fields={LXMF.FIELD_IMAGE: ["png", b"image"]})
    chat.on_delivery(message)
    receive.assert_not_called()
    save.assert_not_called()
    assert chat.active_peer is None and not chat.peers


def test_verified_incoming_message_does_not_redirect_draft(chat, capsys):
    chat.active_peer = "b" * 32
    chat.on_delivery(SimpleNamespace(signature_validated=True, source_hash=b"\xaa" * 16,
                                     content=b"hello\x1b[2J", fields={}))
    assert chat.active_peer == "b" * 32
    assert "\\x1b[2J" in capsys.readouterr().out


def test_first_incoming_message_requires_explicit_recipient_selection(chat):
    chat.on_delivery(SimpleNamespace(signature_validated=True, source_hash=b"\xaa" * 16,
                                     content=b"hello", fields={}))
    assert chat.active_peer is None
    assert "aa" * 16 in chat.peers


def test_invalid_attachment_does_not_hide_valid_attachments(chat):
    chat.on_delivery(SimpleNamespace(signature_validated=True, source_hash=b"\xaa" * 16,
                                     content=b"hello", fields={LXMF.FIELD_FILE_ATTACHMENTS: [
                                         ["../bad", b"bad"], ["good.png", b"good"]]}))
    assert len(chat._images) == 1
    assert Path(chat._images[0]).read_bytes() == b"good"


def test_attachment_count_is_bounded(chat):
    chat.on_delivery(SimpleNamespace(signature_validated=True, source_hash=b"\xaa" * 16,
                                     content=b"", fields={LXMF.FIELD_FILE_ATTACHMENTS: [
                                         ["same.png", b"image"]] * 25}))
    assert len(chat._images) == chat.MAX_ATTACHMENTS_PER_MESSAGE


@pytest.mark.parametrize("data", [
    b"Alice", msgpack.packb([b"Alice", None]), msgpack.packb([b"Alice", None, [0]]),
    msgpack.packb([b"A" * 300, None, [0]]),
])
def test_official_announce_formats(chat, data):
    chat.AnnounceHandler().received_announce(b"\xaa" * 16, RNS.Identity(), data)
    assert chat.peers["aa" * 16] in {"Alice", "A" * 300}


def test_malformed_announce_is_ignored(chat):
    chat.AnnounceHandler().received_announce(b"\xaa" * 16, RNS.Identity(), b"\x93")
    assert not chat.peers


def test_announce_refreshes_global_prompt_and_escapes_name(chat):
    chat.active_peer = "aa" * 16
    chat.AnnounceHandler().received_announce(b"\xaa" * 16, RNS.Identity(),
                                            msgpack.packb([b"Alice\x1b[2J", None, [0]]))
    assert "Alice" in chat.current_prompt
    assert "\\x1b[2J" in chat.current_prompt
    assert "\x1b[2J" not in chat.current_prompt


def test_contacts_are_atomic_and_private(chat):
    chat.cmd_to("a" * 32)
    chat.cmd_rename("a" * 32 + " Alice")
    chat.cmd_save()
    assert json.loads(chat._names_path().read_text()) == {"a" * 32: "Alice"}
    assert chat._saved_path().stat().st_mode & 0o777 == 0o600


def test_failed_atomic_contact_write_keeps_original(chat, monkeypatch):
    path = chat._names_path()
    path.write_text(json.dumps({"a" * 32: "Alice"}))
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("disk problem")))
    with pytest.raises(OSError):
        chat._write_mapping(path, {"b" * 32: "Bob"})
    assert json.loads(path.read_text()) == {"a" * 32: "Alice"}
    assert list(path.parent.iterdir()) == [path]


def test_corrupt_contacts_are_not_overwritten(chat):
    path = chat._names_path()
    path.write_text("{bad json")
    chat._load_custom_names()
    chat.cmd_rename("a" * 32 + " Alice")
    assert path.read_text() == "{bad json"
    assert not chat.custom_names
