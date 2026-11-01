#!/usr/bin/env python3
"""Minimal live Reticulum-link chat demo; use lxmf_chat.py for interoperable messaging."""

import argparse
import random
import sys
import threading
import time

import RNS

from chat_utils import address_argument, console_input, load_identity, normalize_address, safe_text


APP_NAME = "meshchat"
ASPECT = "messenger"
MIN_ANNOUNCE_INTERVAL = 600
_links: dict[str, RNS.Link] = {}
_listed_links: list[str] = []
_active_link: str | None = None
_lock = threading.RLock()
_stop_event = threading.Event()
_announcer: threading.Thread | None = None
_client_mode = False
_last_announce = float("-inf")


def _print(message: str) -> None:
    with _lock:
        print(message, flush=True)


def _peer_label(link: RNS.Link) -> str:
    identity = link.get_remote_identity()
    if identity is None and link.initiator:
        # The responder is authenticated by the destination/link proof, not by
        # link.identify(), which can only identify a link's initiator.
        identity = link.destination.identity
    return RNS.prettyhexrep(identity.hash) if identity else "unidentified peer"


def packet_received(data: bytes, packet: RNS.Packet) -> None:
    link = packet.link
    if not link.initiator and link.get_remote_identity() is None:
        _print("[!] Ignoring a message from a peer that has not identified itself")
        return
    text = safe_text(data.decode("utf-8", errors="replace"), multiline=True)
    _print(f"[{_peer_label(link)}] >> {text}")


def link_closed(link: RNS.Link) -> None:
    """A peer owns its session, never the lifetime of the listening server."""
    global _active_link
    key = link.link_id.hex()
    with _lock:
        _links.pop(key, None)
        if _active_link == key:
            _active_link = None
    _print(f"[!] Link {key[:12]} closed (reason: {safe_text(link.teardown_reason)})")
    if _client_mode:
        _stop_event.set()
        _print("[*] Connection closed; press Enter to exit")


def _register_link(link: RNS.Link) -> None:
    with _lock:
        _links[link.link_id.hex()] = link
    link.set_packet_callback(packet_received)
    link.set_link_closed_callback(link_closed)


def _remote_identified(link: RNS.Link, identity: RNS.Identity) -> None:
    _print(f"[*] Peer {RNS.prettyhexrep(identity.hash)} identified on {link.link_id.hex()[:12]}; "
           "use /peers and /to <index> to select it")


def _list_peers() -> None:
    global _listed_links
    with _lock:
        _listed_links = list(_links)
        rows = [(key, _peer_label(_links[key])) for key in _listed_links]
    if not rows:
        _print("[*] No connected peers")
    for index, (key, label) in enumerate(rows, 1):
        _print(f"{index}: {label} (link {key[:12]})")


def _select_peer(value: str) -> None:
    global _active_link
    if not value.isascii() or not value.isdigit() or len(value) > 9:
        raise ValueError("Usage: /to <index from /peers>")
    index = int(value)
    with _lock:
        keys = _listed_links or list(_links)
        if not 1 <= index <= len(keys) or keys[index - 1] not in _links:
            raise ValueError("Peer disconnected or index out of range; use /peers")
        key = keys[index - 1]
        link = _links[key]
        if not link.initiator and link.get_remote_identity() is None:
            raise ValueError("Wait for the peer to identify itself")
        _active_link = key
    _print(f"[*] Active peer: {_peer_label(link)}")


def _announce(destination: RNS.Destination) -> None:
    global _last_announce
    with _lock:
        elapsed = time.monotonic() - _last_announce
        if elapsed < MIN_ANNOUNCE_INTERVAL:
            _print(f"[!] Announce cooldown: wait {MIN_ANNOUNCE_INTERVAL - elapsed:.0f}s")
            return
        destination.announce()
        _last_announce = time.monotonic()


def _announce_loop(destination: RNS.Destination, interval: int) -> None:
    while not _stop_event.wait(max(MIN_ANNOUNCE_INTERVAL, interval * random.uniform(0.9, 1.1))):
        try:
            _announce(destination)
        except Exception as exc:
            _print(f"[!] Announce failed: {safe_text(exc)}")


def _prompt() -> str:
    with _lock:
        label = _active_link[:12] if _active_link else "no peer"
    return f"[{label}] > "


def input_loop(link: RNS.Link | None = None, destination: RNS.Destination | None = None) -> None:
    """Only the main thread reads stdin, even with multiple connected peers."""
    global _active_link
    if link is not None:
        with _lock:
            _active_link = link.link_id.hex()
    with console_input(_prompt) as read_line:
        while not _stop_event.is_set():
            try:
                text = read_line()
                if not text.strip():
                    continue
                if text.startswith("/"):
                    parts = text[1:].split(None, 1)
                    command = parts[0].lower() if parts else "help"
                    args = parts[1] if len(parts) > 1 else ""
                    if command in {"quit", "exit", "q"}:
                        break
                    if command == "peers":
                        _list_peers()
                    elif command == "to":
                        _select_peer(args)
                    elif command == "announce" and destination is not None:
                        _announce(destination)
                    else:
                        _print("Commands: /peers, /to <index>, /announce (server), /quit")
                    continue
                with _lock:
                    selected = _links.get(_active_link)
                if selected is None or selected.status != RNS.Link.ACTIVE:
                    _print("[!] Select a connected peer with /peers and /to <index>")
                    continue
                data = text.encode("utf-8")
                if len(data) > selected.mdu:
                    _print(f"[!] Message too long ({selected.mdu} bytes maximum); use LXMF for longer messages")
                    continue
                if RNS.Packet(selected, data).send() is False:
                    _print("[!] Packet could not be sent")
            except (EOFError, KeyboardInterrupt):
                break
            except (OSError, ValueError) as exc:
                _print(f"[!] {safe_text(exc)}")


def run_server(rns_config: str | None = None, identity_path: str = "./server.id",
               announce_interval: int = 3600) -> None:
    global _announcer
    server_id = load_identity(identity_path)
    RNS.Reticulum(configdir=rns_config)
    destination = RNS.Destination(server_id, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, ASPECT)

    def link_established(link: RNS.Link) -> None:
        _register_link(link)
        link.set_remote_identified_callback(_remote_identified)
        _print("[*] New link established; waiting for the peer to identify itself")

    destination.set_link_established_callback(link_established)
    _print(f"[*] Server identity: {RNS.prettyhexrep(server_id.hash)}")
    _print(f"[*] Destination hash: {destination.hash.hex()}")
    _print("[*] Waiting for clients; use /peers, /to <index>, /quit")
    _announce(destination)
    if announce_interval:
        _announcer = threading.Thread(target=_announce_loop, args=(destination, announce_interval), daemon=True)
        _announcer.start()
    input_loop(destination=destination)


def run_client(destination_hex: str, rns_config: str | None = None,
               identity_path: str = "./client.id", timeout: float = 30) -> None:
    global _client_mode
    target_hash = bytes.fromhex(normalize_address(destination_hex))
    client_id = load_identity(identity_path)
    _client_mode = True
    RNS.Reticulum(configdir=rns_config)
    _print(f"[*] Your identity: {RNS.prettyhexrep(client_id.hash)}")
    deadline = time.monotonic() + timeout
    if not RNS.Transport.has_path(target_hash):
        RNS.Transport.request_path(target_hash)
        _print("[*] Requesting path to server...")
    while not RNS.Transport.has_path(target_hash):
        if time.monotonic() >= deadline or _stop_event.wait(0.1):
            raise TimeoutError("Timed out discovering the server; check its destination hash")
    server_identity = RNS.Identity.recall(target_hash)
    if server_identity is None:
        raise ValueError("The server path has no known identity; retry after its next announce")
    destination = RNS.Destination(server_identity, RNS.Destination.OUT, RNS.Destination.SINGLE, APP_NAME, ASPECT)
    ready = threading.Event()

    def established(link: RNS.Link) -> None:
        _register_link(link)
        link.identify(client_id)
        ready.set()

    _print("[*] Establishing encrypted link...")
    link = RNS.Link(destination, established_callback=established, closed_callback=link_closed)
    deadline = time.monotonic() + timeout
    while not ready.is_set():
        if link.status == RNS.Link.CLOSED:
            raise ConnectionError("Server link closed before establishment")
        if time.monotonic() >= deadline or _stop_event.wait(0.1):
            link.teardown()
            raise TimeoutError("Timed out establishing the server link")
    _print("[*] Connected; type a message or /quit")
    input_loop(link)


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", nargs="?", type=address_argument,
                        help="Server destination hash; omit to listen")
    parser.add_argument("--identity", help="Private identity file (default: server.id or client.id)")
    parser.add_argument("--rns-config", help="Reticulum configuration directory")
    parser.add_argument("--timeout", type=_positive_int, default=30, help="Path/link timeout in seconds")
    parser.add_argument("--announce-interval", type=_announce_interval, default=3600,
                        help="Seconds between server announces (0 disables periodic announces)")
    args = parser.parse_args()
    _stop_event.clear()
    try:
        if args.destination:
            run_client(args.destination, args.rns_config, args.identity or "./client.id", args.timeout)
        else:
            run_server(args.rns_config, args.identity or "./server.id", args.announce_interval)
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        _print(f"[!] {safe_text(exc)}")
        return 1
    finally:
        _stop_event.set()
        with _lock:
            links = list(_links.values())
        for link in links:
            link.teardown()
        if _announcer is not None:
            _announcer.join(timeout=2)


if __name__ == "__main__":
    sys.exit(main())
