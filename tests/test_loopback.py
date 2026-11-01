"""Integration tests use real RNS/LXMF in separate processes, never public peers."""

import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time

import RNS

from chat_utils import load_identity


ROOT = Path(__file__).resolve().parents[1]


def loopback_configs(directory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    common = "[reticulum]\n  share_instance = no\n  enable_transport = no\n[logging]\n  loglevel = 0\n[interfaces]\n"
    server = directory / "server-config"
    client = directory / "client-config"
    server.mkdir()
    client.mkdir()
    (server / "config").write_text(common + f"""
  [[Loopback only]]
    type = TCPServerInterface
    enabled = yes
    listen_ip = 127.0.0.1
    listen_port = {port}
""")
    (client / "config").write_text(common + f"""
  [[Loopback only]]
    type = TCPClientInterface
    enabled = yes
    target_host = 127.0.0.1
    target_port = {port}
""")
    return server, client


def wait_until(predicate, process, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        if process.poll() is not None:
            raise AssertionError(f"Peer exited early: {process.communicate()[0]}")
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for isolated peer")


def finish(process):
    if process.poll() is None:
        process.terminate()
    try:
        return process.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate()[0]


def test_real_lxmf_delivery_and_signature_verification(tmp_path):
    server_config, client_config = loopback_configs(tmp_path)
    receiver_storage = tmp_path / "receiver"
    sender_storage = tmp_path / "sender"
    helper = str(ROOT / "tests" / "loopback_peer.py")
    receiver = subprocess.Popen([sys.executable, helper, "receiver", str(server_config),
                                 str(receiver_storage), "unused"], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_until(lambda: (receiver_storage / "ready").exists()
                   and len((receiver_storage / "ready").read_text()) == 32, receiver)
        address = (receiver_storage / "ready").read_text()
        sender = subprocess.run([sys.executable, helper, "sender", str(client_config),
                                 str(sender_storage), address], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=30)
        assert sender.returncode == 0, sender.stdout
        assert json.loads((sender_storage / "result.json").read_text()) == {"status": "delivered"}
        wait_until(lambda: (receiver_storage / "received.txt").exists()
                   and (receiver_storage / "received.txt").read_text() == "Hello over isolated loopback LXMF", receiver)
        assert (receiver_storage / "received.txt").read_text() == "Hello over isolated loopback LXMF"
    finally:
        if receiver_storage.exists():
            (receiver_storage / "stop").touch()
        finish(receiver)


def test_real_raw_server_survives_disconnect_and_reconnect(tmp_path):
    server_config, client_config = loopback_configs(tmp_path)
    script = str(ROOT / "meshchat.py")
    server = subprocess.Popen([sys.executable, script, "--rns-config", str(server_config),
                               "--identity", str(tmp_path / "server.id"), "--announce-interval", "0"],
                              cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    output = []

    def collect():
        for line in server.stdout:
            output.append(line)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        wait_until(lambda: "Destination hash:" in "".join(output), server)
        address = re.search(r"Destination hash: ([0-9a-f]{32})", "".join(output)).group(1)
        for _ in range(2):
            client = subprocess.run([sys.executable, script, address, "--rns-config", str(client_config),
                                     "--identity", str(tmp_path / "client.id"), "--timeout", "5"],
                                    cwd=ROOT, input="hello from loopback\n/quit\n",
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, timeout=15)
            assert client.returncode == 0, client.stdout
            assert "Connected" in client.stdout
            assert server.poll() is None, "A remote disconnect stopped the server"
        wait_until(lambda: "".join(output).count("hello from loopback") == 2, server)
        server.stdin.write("/quit\n")
        server.stdin.flush()
        server.wait(timeout=10)
        assert server.returncode == 0, "".join(output)
    finally:
        # stdout is consumed by the reader, so do not call communicate concurrently.
        if server.poll() is None:
            server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        reader.join(timeout=2)
        server.stdin.close()
        server.stdout.close()


def test_real_propagation_fallback_and_offline_sync(tmp_path):
    node_config, sender_config = loopback_configs(tmp_path)
    config_file = node_config / "config"
    config_file.write_text(config_file.read_text().replace("enable_transport = no", "enable_transport = yes"))
    receiver_config = tmp_path / "receiver-config"
    receiver_config.mkdir()
    (receiver_config / "config").write_text((sender_config / "config").read_text())
    node_storage = tmp_path / "node"
    sender_storage = tmp_path / "sender"
    receiver_storage = tmp_path / "receiver"
    peers = []
    for storage in (sender_storage, receiver_storage):
        storage.mkdir()
        identity = load_identity(storage / "identity")
        destination = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE, "lxmf", "delivery")
        peers.append({"address": destination.hash.hex(), "public_key": identity.get_public_key().hex()})
    metadata = tmp_path / "metadata.json"
    metadata.write_text("{}")
    helper = str(ROOT / "tests" / "propagation_peer.py")
    node = subprocess.Popen([sys.executable, helper, "node", str(node_config), str(node_storage), str(metadata)],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        wait_until(lambda: (node_storage / "ready").exists()
                   and len((node_storage / "ready").read_text()) == 32, node)
        metadata.write_text(json.dumps({"node": (node_storage / "ready").read_text(),
                                        "peers": peers, "recipient": peers[1]["address"]}))
        sender = subprocess.run([sys.executable, helper, "sender", str(sender_config), str(sender_storage), str(metadata)],
                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
        assert sender.returncode == 0, sender.stdout
        assert (sender_storage / "result").read_text() == "propagated"
        receiver = subprocess.run([sys.executable, helper, "receiver", str(receiver_config), str(receiver_storage), str(metadata)],
                                  cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=25)
        assert receiver.returncode == 0, receiver.stdout
        assert (receiver_storage / "received").read_text() == "Stored while the recipient was offline"
    finally:
        if node_storage.exists():
            (node_storage / "stop").touch()
        finish(node)
