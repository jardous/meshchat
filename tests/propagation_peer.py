"""Isolated real propagation/sync peers; no public interfaces or peers are used."""

import json
from pathlib import Path
import sys
import time

import LXMF
import LXMF.LXStamper as stamper
import RNS

import lxmf_chat as chat
from chat_utils import load_identity


def main():
    role, config, storage_name, metadata_name = sys.argv[1:]
    storage = Path(storage_name)
    storage.mkdir(exist_ok=True)
    metadata = json.loads(Path(metadata_name).read_text())
    # Generate genuine proof-of-work with LXMF's single-core algorithm, without
    # starting a worker for every CPU on the CI runner.
    stamper.USE_WORKER_MANAGER = False
    stamper.job_linux = stamper.job_simple
    LXMF.LXMRouter.PROCESSING_INTERVAL = 0.1
    LXMF.LXMRouter.NODE_ANNOUNCE_DELAY = 0.05
    chat._path_timeout = 5
    node = None
    try:
        if role == "node":
            RNS.Reticulum(configdir=config)
            node = LXMF.LXMRouter(identity=load_identity(storage / "identity"),
                                  storagepath=str(storage), autopeer=False, propagation_cost=13)
            node.enable_propagation()
            (storage / "ready").write_text(node.propagation_destination.hash.hex())
            deadline = time.monotonic() + 40
            while not (storage / "stop").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            return 0

        chat.init(str(storage), role, config, metadata["node"], announce_interval=0)
        for peer in metadata["peers"]:
            RNS.Identity.remember(None, bytes.fromhex(peer["address"]), bytes.fromhex(peer["public_key"]), None)
        if role == "sender":
            # Deterministically exhaust only direct attempts. The real router
            # invokes its failure callback under its own lock; fallback, stamps,
            # encryption, TCP transfer and node storage remain entirely real.
            attempt_limit = chat.router.attempt_limit
            chat.router.attempt_limit = lambda msg: (-1 if msg.method == LXMF.LXMessage.DIRECT
                                                      else attempt_limit(msg))
            message_id = chat.send_message(metadata["recipient"], "Stored while the recipient was offline")
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                status = chat._outgoing[message_id].status
                if status in chat.TERMINAL_STATES:
                    (storage / "result").write_text(status)
                    return 0 if status == "propagated" else 1
                time.sleep(0.05)
            raise TimeoutError("Propagation did not finish")

        def received(message):
            chat.on_delivery(message)
            if message.signature_validated:
                (storage / "received").write_text(message.content_as_string())

        chat.router.register_delivery_callback(received)
        chat.cmd_sync()
        deadline = time.monotonic() + 20
        while not (storage / "received").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not (storage / "received").exists():
            raise TimeoutError(f"Sync did not deliver a verified message; state={chat.router.propagation_transfer_state:#x}")
        return 0
    finally:
        if node is not None:
            node.exit_handler()
        else:
            chat.shutdown()


if __name__ == "__main__":
    sys.exit(main())
