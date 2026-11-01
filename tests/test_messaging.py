import queue
from types import SimpleNamespace
from unittest.mock import Mock

import LXMF
import pytest
import RNS


def test_sending_is_queued_not_reported_delivered(chat, fake_router, capsys):
    message_id = chat.send_message("a" * 32, "hello")
    assert chat._outgoing[message_id].status == "queued"
    assert chat._jobs.qsize() == 1
    fake_router.handle_outbound.assert_not_called()
    assert "delivered" not in capsys.readouterr().out


def test_unknown_identity_requests_path_and_times_out(chat, fake_router, monkeypatch):
    monkeypatch.setattr(RNS.Identity, "recall", Mock(return_value=None))
    request = Mock()
    monkeypatch.setattr(RNS.Transport, "request_path", request)
    message_id = chat.send_message("a" * 32, "hello")
    chat._jobs.get_nowait()()
    request.assert_called_once_with(bytes.fromhex("a" * 32))
    assert chat._outgoing[message_id].status == "failed"
    fake_router.handle_outbound.assert_not_called()


def test_identity_discovery_can_complete(chat, monkeypatch):
    identity = RNS.Identity()
    monkeypatch.setattr(RNS.Identity, "recall", Mock(side_effect=[None, identity]))
    request = Mock()
    monkeypatch.setattr(RNS.Transport, "request_path", request)
    assert chat._resolve_identity(b"\xaa" * 16) is identity
    request.assert_called_once()


def test_direct_send_reports_acknowledged_delivery(chat, fake_router, monkeypatch):
    monkeypatch.setattr(RNS.Identity, "recall", Mock(return_value=RNS.Identity()))
    message_id = chat.send_message("a" * 32, "hello")
    chat._jobs.get_nowait()()
    message = fake_router.handle_outbound.call_args.args[0]
    assert message.desired_method == LXMF.LXMessage.DIRECT
    assert chat._outgoing[message_id].status == "sending"
    message.method = LXMF.LXMessage.DIRECT
    message.state = LXMF.LXMessage.DELIVERED
    # Invoke the actual LXMF-registered callback to simulate its delivery receipt.
    message._LXMessage__delivery_callback(message)
    assert chat._outgoing[message_id].status == "delivered"


def test_direct_failure_queues_one_fallback_without_reentering_router(chat, fake_router, monkeypatch):
    monkeypatch.setattr(RNS.Identity, "recall", Mock(return_value=RNS.Identity()))
    fake_router.get_outbound_propagation_node.return_value = b"\xbb" * 16
    message_id = chat.send_message("a" * 32, "hello")
    chat._jobs.get_nowait()()
    direct = fake_router.handle_outbound.call_args.args[0]
    direct.state = LXMF.LXMessage.FAILED
    direct.failed_callback(direct)
    assert fake_router.handle_outbound.call_count == 1
    assert chat._outgoing[message_id].status == "queued for propagation"
    chat._jobs.get_nowait()()
    propagated = fake_router.handle_outbound.call_args.args[0]
    assert propagated.desired_method == LXMF.LXMessage.PROPAGATED
    assert fake_router.handle_outbound.call_count == 2
    propagated.method = LXMF.LXMessage.PROPAGATED
    propagated._LXMessage__delivery_callback(propagated)
    assert chat._outgoing[message_id].status == "propagated"
    propagated.state = LXMF.LXMessage.FAILED
    propagated.failed_callback(propagated)
    assert chat._outgoing[message_id].status == "failed"
    assert chat._jobs.empty()


def test_fallback_preserves_wire_message_hash(chat, fake_router, monkeypatch):
    recipient = RNS.Identity()
    monkeypatch.setattr(RNS.Identity, "recall", Mock(return_value=recipient))
    monkeypatch.setattr(RNS.Identity, "recall_app_data", Mock(return_value=None))
    fake_router.get_outbound_propagation_node.return_value = b"\xbb" * 16
    outgoing = chat.OutgoingMessage("a" * 32, "same logical message")
    chat._send_outgoing(outgoing)
    direct = fake_router.handle_outbound.call_args.args[0]
    direct.pack()
    direct.state = LXMF.LXMessage.FAILED
    direct.failed_callback(direct)
    chat._jobs.get_nowait()()
    propagated = fake_router.handle_outbound.call_args.args[0]
    propagated.pack()
    assert propagated.hash == direct.hash


@pytest.mark.parametrize("state, expected", [
    (LXMF.LXMessage.REJECTED, "rejected"), (LXMF.LXMessage.CANCELLED, "cancelled"),
])
def test_rejected_or_cancelled_messages_never_fallback(chat, fake_router, state, expected):
    fake_router.get_outbound_propagation_node.return_value = b"\xbb" * 16
    outgoing = chat.OutgoingMessage("a" * 32, "hello")
    chat._delivery_failed(outgoing, SimpleNamespace(state=state, desired_method=LXMF.LXMessage.DIRECT))
    assert outgoing.status == expected
    assert chat._jobs.empty()


def test_failure_without_propagation_node_is_visible(chat, fake_router):
    outgoing = chat.OutgoingMessage("a" * 32, "hello")
    chat._delivery_failed(outgoing, SimpleNamespace(state=LXMF.LXMessage.FAILED,
                                                   desired_method=LXMF.LXMessage.DIRECT))
    assert outgoing.status == "failed" and chat._jobs.empty()


def test_sync_uses_configured_propagation_node(chat, fake_router):
    fake_router.get_outbound_propagation_node.return_value = b"\xbb" * 16
    chat.cmd_sync()
    fake_router.request_messages_from_propagation_node.assert_not_called()
    chat._jobs.get_nowait()()
    fake_router.request_messages_from_propagation_node.assert_called_once_with(chat.local_identity)


def test_sync_without_node_does_not_queue(chat, fake_router, capsys):
    chat.cmd_sync()
    assert chat._jobs.empty()
    assert "--propagation-node" in capsys.readouterr().out


def test_full_background_queue_fails_visibly(chat, fake_router, monkeypatch):
    jobs = queue.Queue(maxsize=1)
    jobs.put(lambda: None)
    monkeypatch.setattr(chat, "_jobs", jobs)
    message_id = chat.send_message("a" * 32, "hello")
    assert chat._outgoing[message_id].status == "failed"


def test_announce_has_cooldown(chat, fake_router):
    chat.cmd_announce()
    chat.cmd_announce()
    fake_router.announce.assert_called_once_with(chat.local_dest.hash)


def test_init_uses_returned_delivery_destination(chat, fake_router, monkeypatch, tmp_path):
    monkeypatch.setattr(RNS.Reticulum, "__init__", lambda *args, **kwargs: None)
    monkeypatch.setattr(RNS.Transport, "register_announce_handler", Mock())
    monkeypatch.setattr(LXMF, "LXMRouter", Mock(return_value=fake_router))
    chat.init(str(tmp_path), "Alice", None, "b" * 32, announce_interval=0)
    assert chat.local_dest is fake_router.register_delivery_identity.return_value
    fake_router.set_outbound_propagation_node.assert_called_once_with(bytes.fromhex("b" * 32))
    chat.shutdown()
    fake_router.exit_handler.assert_called_once()


def test_shutdown_stops_work_and_marks_pending_messages(chat, fake_router):
    message_id = chat.send_message("a" * 32, "hello")
    chat.shutdown()
    assert chat._outgoing[message_id].status == "cancelled"
    assert not chat._queue_work(lambda: None)
    fake_router.exit_handler.assert_called_once()
