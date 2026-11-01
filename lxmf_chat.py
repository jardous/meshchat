#!/usr/bin/env python3
"""A lightweight LXMF console client compatible with MeshChat, Sideband and NomadNet."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable
import uuid

import LXMF
import RNS

from chat_utils import address_argument, console_input, load_identity, normalize_address, safe_text


_tty = sys.stdout.isatty()
C_RESET = "\033[0m" if _tty else ""
C_BOLD = "\033[1m" if _tty else ""
C_DIM = "\033[2m" if _tty else ""
C_GREEN = "\033[32m" if _tty else ""
C_BLUE = "\033[34m" if _tty else ""

MIN_ANNOUNCE_INTERVAL = 600
DEFAULT_ANNOUNCE_INTERVAL = 3600
MAX_OUTGOING = 200
MAX_ATTACHMENTS_PER_MESSAGE = 20
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff"}
TERMINAL_STATES = {"delivered", "propagated", "failed", "rejected", "cancelled"}

router: LXMF.LXMRouter | None = None
local_dest: RNS.Destination | None = None
local_identity: RNS.Identity | None = None
display_name = "LXMFChat"
active_peer: str | None = None
peers: dict[str, str] = {}
custom_names: dict[str, str] = {}
current_prompt = "[no peer] "
_storage_path = ""
_images: list[str] = []
_listed_peers: list[str] = []
_state_lock = threading.RLock()
_print_lock = threading.Lock()
_attachment_lock = threading.Lock()
_announce_lock = threading.Lock()
_stop_event = threading.Event()
_jobs: queue.Queue[Callable[[], None]] = queue.Queue(maxsize=32)
_worker: threading.Thread | None = None
_announcer: threading.Thread | None = None
_last_announce = float("-inf")
_path_timeout = 30.0
_max_attachment_bytes = 1024 * 1024
_attachment_quota_bytes = 50 * 1024 * 1024


@dataclass
class OutgoingMessage:
    """UI state is independent of the LXMF attempt used to deliver a message."""

    peer: str
    content: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)
    status: str = "queued"
    fallback_attempted: bool = False


_outgoing: dict[str, OutgoingMessage] = {}


def _print(line: str) -> None:
    # prompt_toolkit.patch_stdout restores both the prompt and an unfinished draft.
    with _print_lock:
        print(line, flush=True)


def ts() -> str:
    return time.strftime("%H:%M:%S")


def info(msg: str) -> None:
    _print(f"{C_DIM}[{ts()}]{C_RESET} {msg}")


def recv(who: str, msg: str) -> None:
    _print(f"{C_DIM}[{ts()}]{C_RESET} {C_GREEN}{C_BOLD}{safe_text(who)}"
           f"{C_RESET} > {safe_text(msg, multiline=True)}")


def warn(msg: str) -> None:
    info(f"! {safe_text(msg)}")


def err(msg: str) -> None:
    info(f"ERROR: {safe_text(msg)}")


def _display_name_for(address: str) -> str:
    with _state_lock:
        return safe_text(custom_names.get(address) or peers.get(address) or "")


def _refresh_prompt() -> None:
    global current_prompt
    with _state_lock:
        label = (_display_name_for(active_peer) or active_peer) if active_peer else "no peer"
        current_prompt = f"{C_DIM}[{label}]{C_RESET} "


def _names_path() -> Path:
    return Path(_storage_path) / "peer_names.json"


def _saved_path() -> Path:
    return Path(_storage_path) / "saved_peers.json"


def _read_mapping(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                         for key, value in data.items()):
        raise ValueError("Expected an address-to-name object")
    return {normalize_address(key): safe_text(value) for key, value in data.items()}


def _load_mapping(path: Path) -> dict[str, str]:
    try:
        return _read_mapping(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        warn(f"Cannot load {path}: {exc}; keeping the file for recovery")
        return {}


def _write_mapping(path: Path, data: dict[str, str]) -> None:
    # Atomic replacement leaves the previous contacts intact on interrupted writes.
    if path.exists():
        _read_mapping(path)  # Do not overwrite corrupt data that was kept for recovery.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load_custom_names() -> None:
    with _state_lock:
        custom_names.update(_load_mapping(_names_path()))


def _save_custom_names() -> None:
    with _state_lock:
        _write_mapping(_names_path(), dict(custom_names))


def _load_saved_peers() -> dict[str, str]:
    return _load_mapping(_saved_path())


def _save_peers_file(data: dict[str, str]) -> None:
    _write_mapping(_saved_path(), data)


def _add_image(path: str) -> int:
    with _state_lock:
        _images.append(path)
        return len(_images)


def _save_attachment(sender_hex: str, filename: str, data: bytes) -> str:
    """Store bounded data under a unique, exclusive, private file in attachments/."""
    sender_hex = normalize_address(sender_hex)
    if not isinstance(data, bytes):
        raise ValueError("Attachment payload must be bytes")
    if len(data) > _max_attachment_bytes:
        raise ValueError("Attachment exceeds the per-file size limit")
    if not isinstance(filename, str) or not filename or filename in {".", ".."}:
        raise ValueError("Invalid attachment filename")
    if any(char in filename for char in ("/", "\\", "\x00")):
        raise ValueError("Attachment filename must not contain path separators or NUL")
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)[:120]
    with _attachment_lock:
        directory = Path(_storage_path) / "attachments"
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        # Account for empty/tiny files too, so they cannot bypass the storage quota.
        used = sum(max(4096, path.stat().st_size) for path in directory.iterdir() if path.is_file())
        if used + max(4096, len(data)) > _attachment_quota_bytes:
            raise ValueError("Attachment storage quota reached; remove old attachments first")
        path = directory / f"{sender_hex[:8]}_{uuid.uuid4().hex}_{safe_name}"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
        except OSError:
            path.unlink(missing_ok=True)
            raise
    return str(path)


def _receive_attachment(sender: str, label: str, filename: str, data: bytes) -> None:
    try:
        path = _save_attachment(sender, filename, data)
        if Path(filename).suffix.lower() in IMAGE_EXTENSIONS:
            index = _add_image(path)
            recv(label, f"Image {filename} ({len(data) // 1024} KiB) -> /open {index}")
        else:
            recv(label, f"File {filename} ({len(data) // 1024} KiB) saved: {path}")
    except (OSError, ValueError) as exc:
        warn(f"Attachment rejected: {exc}")


def on_delivery(message: LXMF.LXMessage) -> None:
    # An encrypted transport does not prove the claimed LXMF sender's signature.
    if not message.signature_validated:
        reason = ("unknown sender identity" if message.unverified_reason == LXMF.LXMessage.SOURCE_UNKNOWN
                  else "invalid signature")
        warn(f"Ignoring unverified message ({reason}); no attachments were saved")
        return
    try:
        sender = message.source_hash.hex()
        normalize_address(sender)
        with _state_lock:
            peers.setdefault(sender, "")
        label = _display_name_for(sender) or sender
        content = message.content.decode("utf-8", errors="replace")
        if content:
            recv(label, content)

        count = 0
        image = message.fields.get(LXMF.FIELD_IMAGE)
        if isinstance(image, list) and len(image) == 2 and isinstance(image[0], str):
            extension = "." + image[0].removeprefix("image/").lower()
            if extension in IMAGE_EXTENSIONS:
                _receive_attachment(sender, label, f"image{extension}", image[1])
                count += 1
            else:
                warn("Unsupported image type; image was not saved")

        files = message.fields.get(LXMF.FIELD_FILE_ATTACHMENTS)
        if isinstance(files, list):
            for attachment in files:
                if count >= MAX_ATTACHMENTS_PER_MESSAGE:
                    warn("Too many attachments in one message; remaining attachments ignored")
                    break
                count += 1
                if isinstance(attachment, list) and len(attachment) == 2:
                    _receive_attachment(sender, label, attachment[0], attachment[1])
        # Never change the recipient of a user's unfinished draft on network input.
    except (AttributeError, TypeError, ValueError) as exc:
        err(f"Malformed incoming message: {exc}")


class AnnounceHandler:
    aspect_filter = "lxmf.delivery"

    def received_announce(self, destination_hash: bytes, announced_identity: RNS.Identity,
                         app_data: bytes | None) -> None:
        try:
            address = normalize_address(destination_hash.hex())
            name = safe_text(LXMF.display_name_from_app_data(app_data) or "")
        except Exception as exc:  # Includes MessagePack's own malformed-input exceptions.
            warn(f"Ignoring malformed announce: {exc}")
            return
        # The supplied name is a label, not a verified real-world identity.
        with _state_lock:
            peers[address] = name
            if address == active_peer:
                _refresh_prompt()


def _queue_work(job: Callable[[], None]) -> bool:
    if _stop_event.is_set():
        return False
    try:
        _jobs.put_nowait(job)
        return True
    except queue.Full:
        warn("Background queue is full; try again after pending work completes")
        return False


def _work_loop() -> None:
    while not _stop_event.is_set():
        try:
            job = _jobs.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            if not _stop_event.is_set():
                job()
        except Exception as exc:  # Keep a single failed job from stopping the worker.
            err(f"Background task failed: {exc}")
        finally:
            _jobs.task_done()


def _resolve_identity(destination_hash: bytes) -> RNS.Identity | None:
    identity = RNS.Identity.recall(destination_hash)
    if identity is not None:
        return identity
    RNS.Transport.request_path(destination_hash)
    deadline = time.monotonic() + _path_timeout
    while not _stop_event.is_set():
        identity = RNS.Identity.recall(destination_hash)
        if identity is not None:
            return identity
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        _stop_event.wait(min(0.2, remaining))
    return None


def _set_status(outgoing: OutgoingMessage, status: str) -> None:
    with _state_lock:
        outgoing.status = status
    info(f"[{outgoing.id}] {status} -> {outgoing.peer}")


def _delivery_succeeded(outgoing: OutgoingMessage, message: LXMF.LXMessage) -> None:
    # A propagation-node receipt is not a receipt from the final recipient.
    status = "propagated" if message.method == LXMF.LXMessage.PROPAGATED else "delivered"
    _set_status(outgoing, status)


def _delivery_failed(outgoing: OutgoingMessage, message: LXMF.LXMessage) -> None:
    if _stop_event.is_set() or message.state == LXMF.LXMessage.CANCELLED:
        _set_status(outgoing, "cancelled")
        return
    if message.state == LXMF.LXMessage.REJECTED:
        _set_status(outgoing, "rejected")
        return
    with _state_lock:
        fallback = (not outgoing.fallback_attempted
                    and message.desired_method == LXMF.LXMessage.DIRECT
                    and not getattr(message, "stamp_generation_failed", False)
                    and router is not None
                    and router.get_outbound_propagation_node() is not None)
        if fallback:
            outgoing.fallback_attempted = True
    if fallback:
        _set_status(outgoing, "queued for propagation")
        # LXMF may hold its outbound lock while invoking this callback. Never
        # re-enter handle_outbound synchronously: it would wait for that same lock.
        if not _queue_work(lambda: _send_outgoing(outgoing, LXMF.LXMessage.PROPAGATED)):
            _set_status(outgoing, "failed")
    else:
        _set_status(outgoing, "failed")


def _send_outgoing(outgoing: OutgoingMessage, method: int = LXMF.LXMessage.DIRECT) -> None:
    try:
        identity = _resolve_identity(bytes.fromhex(outgoing.peer))
        if _stop_event.is_set():
            _set_status(outgoing, "cancelled")
            return
        if identity is None:
            _set_status(outgoing, "failed")
            warn(f"No identity for {outgoing.peer} after {_path_timeout:g}s; verify the address and retry")
            return
        destination = RNS.Destination(identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
                                      "lxmf", "delivery")
        message = LXMF.LXMessage(destination, local_dest, outgoing.content, title="",
                                 desired_method=method)
        # Keeping timestamp/content identical preserves the wire message hash,
        # so an ACK lost on direct delivery does not cause a duplicate on fallback.
        message.timestamp = outgoing.timestamp
        message.register_delivery_callback(lambda msg: _delivery_succeeded(outgoing, msg))
        message.register_failed_callback(lambda msg: _delivery_failed(outgoing, msg))
        _set_status(outgoing, "sending" if method == LXMF.LXMessage.DIRECT else "propagating")
        router.handle_outbound(message)
    except Exception as exc:
        # A synchronous router failure may already have scheduled fallback.
        if outgoing.status not in TERMINAL_STATES | {"queued for propagation"}:
            _set_status(outgoing, "failed")
        err(f"Cannot send [{outgoing.id}]: {exc}")


def send_message(dest_hash_hex: str, content: str) -> str | None:
    if router is None or local_dest is None:
        warn("Router not ready")
        return None
    try:
        address = normalize_address(dest_hash_hex)
    except ValueError as exc:
        warn(str(exc))
        return None
    outgoing = OutgoingMessage(address, content)
    with _state_lock:
        for key in list(_outgoing):
            if len(_outgoing) < MAX_OUTGOING:
                break
            if _outgoing[key].status in TERMINAL_STATES:
                del _outgoing[key]
        if len(_outgoing) >= MAX_OUTGOING:
            warn("Too many pending messages; wait for delivery before sending more")
            return None
        _outgoing[outgoing.id] = outgoing
    info(f"{C_BLUE}you{C_RESET} [{outgoing.id}] queued -> {address}: {safe_text(content, multiline=True)}")
    if not _queue_work(lambda: _send_outgoing(outgoing)):
        _set_status(outgoing, "failed")
    return outgoing.id


def cmd_peers() -> None:
    global _listed_peers
    with _state_lock:
        _listed_peers = list(peers)
        rows = [(address, _display_name_for(address)) for address in _listed_peers]
    if not rows:
        info("No peers discovered yet; wait for announces or use /to <address>")
        return
    info(f"{'#':<4} {'Display name':<24} Address")
    for index, (address, name) in enumerate(rows, 1):
        marker = " <-" if address == active_peer else ""
        info(f"{index:<4} {name:<24} {address}{marker}")


def cmd_me() -> None:
    if local_dest is None:
        warn("Not initialised yet")
        return
    info(f"address: {local_dest.hash.hex()}")
    info(f"name: {safe_text(display_name)}")


def _peer_address(token: str) -> str:
    # A full all-digit hex address must not be mistaken for an index.
    if token.isascii() and token.isdigit() and len(token) < 32:
        with _state_lock:
            addresses = _listed_peers or list(peers)
            index = int(token)
            if not 1 <= index <= len(addresses):
                raise ValueError("Peer index out of range; use /peers to list peers")
            return addresses[index - 1]
    return normalize_address(token)


def cmd_to(args: str) -> None:
    global active_peer
    try:
        address = _peer_address(args.strip())
    except ValueError as exc:
        warn(f"{exc}; usage: /to <index|address>")
        return
    with _state_lock:
        active_peer = address
        _refresh_prompt()
    info(f"Active peer -> {_display_name_for(address) or address} ({address})")


def cmd_rename(args: str) -> None:
    tokens = args.strip().split(None, 1)
    if len(tokens) != 2 or not tokens[1].strip():
        warn("Usage: /rename <index|address> <new name>")
        return
    try:
        address = _peer_address(tokens[0])
        name = safe_text(tokens[1].strip())
        with _state_lock:
            previous = custom_names.get(address)
            custom_names[address] = name
            try:
                _save_custom_names()
            except (OSError, ValueError):
                if previous is None:
                    custom_names.pop(address)
                else:
                    custom_names[address] = previous
                raise
            _refresh_prompt()
        info(f"Renamed {address} -> {name}")
    except (OSError, ValueError) as exc:
        warn(f"Cannot rename peer: {exc}")


def cmd_open(args: str) -> None:
    try:
        if not args.isascii() or not args.isdigit() or len(args) > 9:
            raise ValueError("Usage: /open <image index>")
        index = int(args)
        with _state_lock:
            if not 1 <= index <= len(_images):
                raise ValueError("Image index out of range")
            path = _images[index - 1]
        # An argument vector, not a shell command. Never use repr/shlex with os.system.
        subprocess.Popen(["feh", "--", path])
        info(f"Opening {safe_text(path)}")
    except (OSError, ValueError) as exc:
        warn(f"Cannot open image: {exc}")


def cmd_save() -> None:
    with _state_lock:
        address = active_peer
    if address is None:
        warn("No active peer to save")
        return
    try:
        data = _load_saved_peers()
        data[address] = _display_name_for(address)
        _save_peers_file(data)
        info(f"Saved {address}")
    except (OSError, ValueError) as exc:
        err(f"Cannot save peer: {exc}")


def cmd_status() -> None:
    with _state_lock:
        rows = [(item.id, item.status, item.peer) for item in _outgoing.values()]
    if not rows:
        info("No outgoing messages this session")
    for message_id, status, address in rows:
        info(f"[{message_id}] {status} -> {address}")
    if router is not None and router.get_outbound_propagation_node() is not None:
        info(f"Propagation sync state: {router.propagation_transfer_state:#x} "
             f"({router.propagation_transfer_progress:.0%})")


def cmd_sync() -> None:
    if router is None or router.get_outbound_propagation_node() is None:
        warn("Configure --propagation-node <address> to use /sync")
        return

    def sync() -> None:
        router.request_messages_from_propagation_node(local_identity)
        info("Propagation sync requested; use /status for progress")

    _queue_work(sync)


def cmd_announce() -> None:
    global _last_announce
    if router is None or local_dest is None:
        warn("Router not ready")
        return
    with _announce_lock:
        elapsed = time.monotonic() - _last_announce
        if elapsed < MIN_ANNOUNCE_INTERVAL:
            warn(f"Announce cooldown: wait {MIN_ANNOUNCE_INTERVAL - elapsed:.0f}s")
            return
        router.announce(local_dest.hash)
        _last_announce = time.monotonic()
    info("Announced to network")


def cmd_help() -> None:
    for line in (
        "/to <index|address>     Select recipient (addresses are 32 hex characters)",
        "/peers                 List discovered and saved peers",
        "/rename <peer> <name>   Set a persistent local name",
        "/save                  Save the active peer",
        "/me                    Show your address and display name",
        "/announce              Announce, subject to a 10-minute cooldown",
        "/open <index>          Open a received image with feh",
        "/status                Show outgoing delivery and propagation sync status",
        "/sync                  Retrieve messages from the configured propagation node",
        "/quit                  Exit (pending outgoing messages are not persisted)",
        "<text>                 Queue a message for the selected recipient",
    ):
        info(line)


def _announce_loop(interval: int = DEFAULT_ANNOUNCE_INTERVAL) -> None:
    while not _stop_event.wait(max(MIN_ANNOUNCE_INTERVAL, interval * random.uniform(0.9, 1.1))):
        try:
            cmd_announce()
        except Exception as exc:
            warn(f"Periodic announce failed: {exc}")


def init(storage_path: str, name: str, rns_config: str | None,
         propagation_node: str | None = None, announce_interval: int = DEFAULT_ANNOUNCE_INTERVAL) -> None:
    global router, local_dest, local_identity, display_name, _storage_path
    global _worker, _announcer, _last_announce
    _storage_path = str(Path(storage_path).expanduser().resolve())
    directory = Path(_storage_path)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    display_name = safe_text(name)
    _load_custom_names()
    with _state_lock:
        peers.update(_load_saved_peers())

    # Validate/load keys before starting network services; never rotate on corruption.
    local_identity = load_identity(directory / "identity")
    info("Starting Reticulum...")
    RNS.Reticulum(configdir=str(Path(rns_config).expanduser()) if rns_config else None)
    router = LXMF.LXMRouter(identity=local_identity, storagepath=_storage_path)
    local_dest = router.register_delivery_identity(local_identity, display_name=display_name)
    if local_dest is None:
        raise RuntimeError("LXMF did not register the delivery destination")
    router.register_delivery_callback(on_delivery)
    if propagation_node is not None:
        router.set_outbound_propagation_node(bytes.fromhex(normalize_address(propagation_node)))
    RNS.Transport.register_announce_handler(AnnounceHandler())
    _stop_event.clear()
    _last_announce = float("-inf")
    _worker = threading.Thread(target=_work_loop, daemon=True, name="lxmf-outgoing")
    _worker.start()
    cmd_announce()
    if announce_interval:
        _announcer = threading.Thread(target=_announce_loop, args=(announce_interval,),
                                      daemon=True, name="lxmf-announces")
        _announcer.start()


def shutdown() -> None:
    _stop_event.set()
    for thread in (_worker, _announcer):
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
    with _state_lock:
        pending = [item for item in _outgoing.values() if item.status not in TERMINAL_STATES]
        for item in pending:
            item.status = "cancelled"
    if pending:
        warn(f"Exiting with {len(pending)} unfinished message(s); they are not persisted")
    if router is not None:
        router.exit_handler()


def repl() -> None:
    info("LXMF Chat - type /help for commands")
    cmd_me()
    commands = {"help": cmd_help, "peers": cmd_peers, "me": cmd_me,
                "announce": cmd_announce, "save": cmd_save,
                "status": cmd_status, "sync": cmd_sync}
    argument_commands = {"to": cmd_to, "rename": cmd_rename, "open": cmd_open}
    _refresh_prompt()
    with console_input(lambda: current_prompt) as read_line:
        while not _stop_event.is_set():
            try:
                line = read_line().strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line.startswith("/"):
                parts = line[1:].split(None, 1)
                if not parts:
                    warn("Type /help for commands")
                    continue
                command = parts[0].lower()
                args = parts[1] if len(parts) > 1 else ""
                try:
                    if command in {"quit", "exit", "q"}:
                        break
                    if command in argument_commands:
                        argument_commands[command](args)
                    elif command in commands:
                        commands[command]()
                    else:
                        warn(f"Unknown command /{command}; type /help")
                except (OSError, ValueError) as exc:
                    err(f"Command failed: {exc}")
            else:
                with _state_lock:
                    recipient = active_peer
                if recipient is None:
                    warn("No active peer; use /to <address> or /peers")
                else:
                    send_message(recipient, line)
    info("Bye!")


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("Must be a positive integer")
    return number


def _announce_interval(value: str) -> int:
    number = int(value)
    if number != 0 and number < MIN_ANNOUNCE_INTERVAL:
        raise argparse.ArgumentTypeError("Use 0 or at least 600 seconds")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", "-n", default="LXMFChat", help="Public display name")
    parser.add_argument("--to", "-t", dest="peer", type=address_argument, metavar="ADDRESS",
                        help="Initial recipient (32 hex characters)")
    parser.add_argument("--storage", default="~/.lxmf-chat", help="Identity and storage directory")
    parser.add_argument("--rns-config", metavar="DIR", help="Reticulum configuration directory")
    parser.add_argument("--propagation-node", type=address_argument, metavar="ADDRESS",
                        help="LXMF propagation address for fallback and /sync")
    parser.add_argument("--announce-interval", type=_announce_interval,
                        default=DEFAULT_ANNOUNCE_INTERVAL, metavar="SECONDS",
                        help="Periodic announce interval (default: 3600; 0: startup/manual only)")
    parser.add_argument("--path-timeout", type=_positive_int, default=30, metavar="SECONDS",
                        help="Identity discovery timeout (default: 30)")
    parser.add_argument("--max-attachment-kib", type=_positive_int, default=1024,
                        help="Maximum saved attachment size (default: 1024 KiB)")
    parser.add_argument("--attachment-quota-mib", type=_positive_int, default=50,
                        help="Maximum total attachment storage (default: 50 MiB)")
    return parser.parse_args(argv)


def main() -> int:
    global _path_timeout, _max_attachment_bytes, _attachment_quota_bytes
    args = parse_args()
    _path_timeout = args.path_timeout
    _max_attachment_bytes = args.max_attachment_kib * 1024
    _attachment_quota_bytes = args.attachment_quota_mib * 1024 * 1024
    try:
        init(args.storage, args.name, args.rns_config, args.propagation_node, args.announce_interval)
        if args.peer:
            cmd_to(args.peer)
        repl()
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        err(str(exc))
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        shutdown()


if __name__ == "__main__":
    sys.exit(main())
