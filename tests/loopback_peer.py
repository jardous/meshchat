"""A real LXMF peer for subprocess tests, using only the supplied loopback config."""

import json
from pathlib import Path
import sys
import time

import lxmf_chat as chat


def main():
    role, config, storage, target = sys.argv[1:]
    storage = Path(storage)
    chat._path_timeout = 5
    try:
        chat.init(str(storage), f"Loopback {role}", config, announce_interval=0)
        if role == "receiver":
            def received(message):
                chat.on_delivery(message)
                if message.signature_validated:
                    (storage / "received.txt").write_text(message.content_as_string(), encoding="utf-8")

            chat.router.register_delivery_callback(received)
            (storage / "ready").write_text(chat.local_dest.hash.hex(), encoding="ascii")
            deadline = time.monotonic() + 30
            while not (storage / "stop").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            return 0

        message_id = chat.send_message(target, "Hello over isolated loopback LXMF")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = chat._outgoing[message_id].status
            if status in chat.TERMINAL_STATES:
                (storage / "result.json").write_text(json.dumps({"status": status}), encoding="utf-8")
                return 0 if status == "delivered" else 1
            time.sleep(0.05)
        raise TimeoutError("Loopback delivery did not finish")
    finally:
        chat.shutdown()


if __name__ == "__main__":
    sys.exit(main())
