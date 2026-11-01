"""Unit tests must never start the user's Reticulum instance or use public peers."""

import queue
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import RNS

import lxmf_chat
import meshchat


@pytest.fixture(autouse=True)
def no_reticulum_startup(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Unit tests must not start Reticulum")

    monkeypatch.setattr(RNS.Reticulum, "__init__", blocked)


@pytest.fixture
def chat(monkeypatch, tmp_path):
    values = {
        "router": None, "local_dest": None, "local_identity": None,
        "active_peer": None, "current_prompt": "[no peer] ",
        "peers": {}, "custom_names": {}, "_images": [], "_listed_peers": [],
        "_outgoing": {}, "_storage_path": str(tmp_path), "_path_timeout": 0.01,
        "_stop_event": threading.Event(), "_jobs": queue.Queue(maxsize=32),
        "_worker": None, "_announcer": None, "_last_announce": float("-inf"),
        "_max_attachment_bytes": 1024 * 1024, "_attachment_quota_bytes": 50 * 1024 * 1024,
    }
    for name, value in values.items():
        monkeypatch.setattr(lxmf_chat, name, value)
    yield lxmf_chat
    lxmf_chat._stop_event.set()
    for thread in (lxmf_chat._worker, lxmf_chat._announcer):
        if thread is not None and thread.ident is not None:
            thread.join(timeout=2)


@pytest.fixture
def fake_router(chat):
    instance = SimpleNamespace(
        handle_outbound=Mock(), get_outbound_propagation_node=Mock(return_value=None),
        set_outbound_propagation_node=Mock(), register_delivery_callback=Mock(),
        register_delivery_identity=Mock(return_value=SimpleNamespace(hash=b"\xcc" * 16)),
        request_messages_from_propagation_node=Mock(), announce=Mock(), exit_handler=Mock(),
        propagation_transfer_state=0, propagation_transfer_progress=0,
    )
    chat.router = instance
    identity = RNS.Identity()
    chat.local_identity = identity
    chat.local_dest = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
    return instance


@pytest.fixture
def raw(monkeypatch):
    for name, value in {"_links": {}, "_listed_links": [], "_active_link": None,
                        "_client_mode": False, "_stop_event": threading.Event(),
                        "_announcer": None, "_last_announce": float("-inf")}.items():
        monkeypatch.setattr(meshchat, name, value)
    return meshchat
