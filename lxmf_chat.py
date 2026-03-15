#!/usr/bin/env python3
"""
lxmf_chat.py  –  Console LXMF chat for Reticulum MeshChat
============================================================
A jardous/meshchat-style terminal app that is fully compatible with
Reticulum MeshChat (liamcottle), Sideband, and Nomad Network.

Install:
    pip install rns lxmf

Run:
    python lxmf_chat.py                          # auto display name
    python lxmf_chat.py --name "Alice"           # set display name
    python lxmf_chat.py --name "Alice" --to <hash>   # start with a peer

In-app commands:
    /to <hash>       – set the active conversation peer
    /peers           – list all discovered LXMF peers
    /me              – show your LXMF address and display name
    /announce        – re-announce yourself to the network
    /help            – show command list
    /quit  /exit     – quit
    <anything else>  – send as a message to the active peer
"""

import argparse
import os
import sys
import threading
import time

# ── RNS / LXMF ──────────────────────────────────────────────────────────────
try:
    import RNS
    import LXMF
except ImportError:
    sys.exit("Missing packages – run:  pip install rns lxmf")

# ────────────────────────────────────────────────────────────────────────────
# ANSI colours (gracefully disabled on Windows without colorama)
# ────────────────────────────────────────────────────────────────────────────
try:
    import shutil
    _tty = shutil.get_terminal_size().columns > 0
except Exception:
    _tty = False

C_RESET  = "\033[0m"   if _tty else ""
C_BOLD   = "\033[1m"   if _tty else ""
C_DIM    = "\033[2m"   if _tty else ""
C_CYAN   = "\033[36m"  if _tty else ""
C_GREEN  = "\033[32m"  if _tty else ""
C_YELLOW = "\033[33m"  if _tty else ""
C_RED    = "\033[31m"  if _tty else ""
C_BLUE   = "\033[34m"  if _tty else ""

# ────────────────────────────────────────────────────────────────────────────
# Global state
# ────────────────────────────────────────────────────────────────────────────
router:       LXMF.LXMRouter | None = None
local_dest:   RNS.Destination | None = None
local_identity: RNS.Identity | None = None
display_name: str = "LXMFChat"

active_peer:  str | None = None          # hex hash of current conversation
peers:        dict[str, str] = {}        # hash_hex → announced display_name
custom_names: dict[str, str] = {}        # hash_hex → user-set name (persisted)
_storage_path: str = ""


# ────────────────────────────────────────────────────────────────────────────
# Persistent custom peer names
# ────────────────────────────────────────────────────────────────────────────
import json

def _names_path() -> str:
    return os.path.join(_storage_path, "peer_names.json")

def _load_custom_names() -> None:
    try:
        with open(_names_path()) as f:
            custom_names.update(json.load(f))
    except FileNotFoundError:
        pass

def _save_custom_names() -> None:
    with open(_names_path(), "w") as f:
        json.dump(custom_names, f, indent=2)

def _display_name_for(h: str) -> str:
    """Custom name takes priority over announced name."""
    return custom_names.get(h) or peers.get(h) or ""


# ────────────────────────────────────────────────────────────────────────────
# Print helpers  (keep incoming messages from garbling the input prompt)
# ────────────────────────────────────────────────────────────────────────────
_print_lock   = threading.Lock()
current_prompt = ""          # updated by the REPL before each input() call

def _print(line: str) -> None:
    with _print_lock:
        # Erase current input line, print the message, then reprint the prompt
        # so the cursor is back where the user expects it.
        sys.stdout.write("\r\033[K" + line + "\n" + current_prompt)
        sys.stdout.flush()


def ts() -> str:
    return time.strftime("%H:%M:%S")


def info(msg: str)  -> None: _print(f"{C_DIM}[{ts()}]{C_RESET} {C_CYAN}{msg}{C_RESET}")
def recv(who: str, msg: str) -> None:
    _print(f"\x07{C_DIM}[{ts()}]{C_RESET} {C_GREEN}{C_BOLD}{who}{C_RESET}{C_GREEN} >{C_RESET} {msg}")
def sent(msg: str)  -> None: _print(f"{C_DIM}[{ts()}]{C_RESET} {C_BLUE}{C_BOLD}you{C_RESET}{C_BLUE} >{C_RESET} {msg}")
def warn(msg: str)  -> None: _print(f"{C_DIM}[{ts()}]{C_RESET} {C_YELLOW}! {msg}{C_RESET}")
def err(msg: str)   -> None: _print(f"{C_DIM}[{ts()}]{C_RESET} {C_RED}ERROR: {msg}{C_RESET}")


# ────────────────────────────────────────────────────────────────────────────
# LXMF delivery callback  (Reticulum → us)
# ────────────────────────────────────────────────────────────────────────────
def on_delivery(message: LXMF.LXMessage) -> None:
    try:
        content = message.content.decode("utf-8", errors="replace").strip()
        src_hex = message.source_hash.hex()

        # Use known display name, or fall back to hash
        sender_name = _display_name_for(src_hex) or src_hex

        recv(sender_name, content)

        # Auto-set active peer if we don't have one, then refresh the prompt
        global active_peer, current_prompt
        if active_peer is None:
            active_peer = src_hex
            current_prompt = f"{C_DIM}[{sender_name}]{C_RESET} "
            with _print_lock:
                sys.stdout.write("\r\033[K" + current_prompt)
                sys.stdout.flush()

    except Exception as exc:
        err(f"on_delivery: {exc}")


# ────────────────────────────────────────────────────────────────────────────
# Announce handler  (peer discovery)
# ────────────────────────────────────────────────────────────────────────────
class AnnounceHandler:
    aspect_filter = "lxmf.delivery"

    def received_announce(
        self,
        destination_hash: bytes,
        announced_identity: RNS.Identity,
        app_data: bytes | None,
    ) -> None:
        h = destination_hash.hex()
        name = ""
        if app_data:
            try:
                # LXMF app_data is msgpack([display_name_bytes, stamp_cost])
                # Format: 0x92 (fixarray 2) | 0xc4 (bin8) | <len> | <name bytes> | ...
                # Parse without requiring the msgpack package.
                if len(app_data) >= 4 and app_data[0] == 0x92 and app_data[1] == 0xc4:
                    n = app_data[2]
                    name = app_data[3:3 + n].decode("utf-8").strip()
                else:
                    # Older/other clients send raw UTF-8
                    name = app_data.decode("utf-8").strip()
            except Exception:
                pass

        if name:
            peers[h] = name
        elif h not in peers:
            peers[h] = ""
        # Refresh prompt if this is the active peer and no custom name overrides
        if h == active_peer and not custom_names.get(h):
            label = _display_name_for(h) or h
            current_prompt = f"{C_DIM}[{label}]{C_RESET} "
            with _print_lock:
                sys.stdout.write("\r\033[K" + current_prompt)
                sys.stdout.flush()


# ────────────────────────────────────────────────────────────────────────────
# Send an LXMF message
# ────────────────────────────────────────────────────────────────────────────
def send_message(dest_hash_hex: str, content: str) -> None:
    if router is None or local_dest is None:
        warn("Router not ready.")
        return

    try:
        dest_hash = bytes.fromhex(dest_hash_hex)
        identity  = RNS.Identity.recall(dest_hash)

        if identity is None:
            warn(
                f"No path to {dest_hash_hex} yet.  "
                "Wait for their announce or check the hash."
            )
            return

        destination = RNS.Destination(
            identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "lxmf",
            "delivery",
        )

        lxm = LXMF.LXMessage(
            destination,
            local_dest,
            content,
            title          = "",
            desired_method = LXMF.LXMessage.DIRECT,
        )
        lxm.try_propagation_on_fail = True   # fall back to propagation node

        router.handle_outbound(lxm)
        sent(content)

    except Exception as exc:
        err(f"send_message: {exc}")


# ────────────────────────────────────────────────────────────────────────────
# Commands
# ────────────────────────────────────────────────────────────────────────────
def cmd_peers() -> None:
    if not peers:
        info("No peers discovered yet.  Wait for announces or ask them to /announce.")
        return
    info(f"{'─'*72}")
    info(f"  {'#':<4} {'Display name':<20} {'Hash'}")
    info(f"{'─'*72}")
    for i, (h, _) in enumerate(peers.items(), 1):
        marker  = " ◄" if h == active_peer else ""
        display = _display_name_for(h)
        info(f"  {i:<4} {display:<20} {h}{marker}")
    info(f"{'─'*72}")


def cmd_me() -> None:
    if local_dest is None:
        warn("Not initialised yet.")
        return
    h = local_dest.hash.hex()
    info(f"address : {C_BOLD}{h}{C_RESET}")
    info(f"name    : {C_BOLD}{display_name}{C_RESET}")


def cmd_to(args: str) -> None:
    global active_peer, current_prompt
    token = args.strip().split()[0] if args.strip() else ""

    # Allow selecting by index number from /peers
    if token.isdigit():
        idx = int(token)
        peer_list = list(peers.items())
        if idx < 1 or idx > len(peer_list):
            warn(f"Index {idx} out of range – use /peers to list peers.")
            return
        h, name = peer_list[idx - 1]
    else:
        h = token.lower().replace(":", "")
        if len(h) != 32:
            warn("Usage: /to <index>  or  /to <32-char LXMF address>")
            return
        name = _display_name_for(h)

    active_peer    = h
    label          = name or h
    current_prompt = f"{C_DIM}[{label}]{C_RESET} "
    info(f"Active peer → {C_BOLD}{label}{C_RESET}  {h}")


def cmd_rename(args: str) -> None:
    tokens = args.strip().split(None, 1)
    if len(tokens) < 2:
        warn("Usage: /rename <index|hash> <new name>")
        return
    token, new_name = tokens[0], tokens[1].strip()
    if token.isdigit():
        idx = int(token)
        peer_list = list(peers.items())
        if idx < 1 or idx > len(peer_list):
            warn(f"Index {idx} out of range.")
            return
        h = peer_list[idx - 1][0]
    else:
        h = token.lower().replace(":", "")
        if len(h) != 32:
            warn("Usage: /rename <index|hash> <new name>")
            return
    custom_names[h] = new_name
    _save_custom_names()
    info(f"Renamed {h} → {C_BOLD}{new_name}{C_RESET}")
    global current_prompt
    if h == active_peer:
        current_prompt = f"{C_DIM}[{new_name}]{C_RESET} "


def cmd_announce() -> None:
    if router and local_dest:
        router.announce(local_dest.hash)
        info("Announced to network.")
    else:
        warn("Router not ready.")


def cmd_help() -> None:
    info("")
    info(f"  {C_BOLD}/to <index|hash>{C_RESET}  – set active peer by index or 32-char address")
    info(f"  {C_BOLD}/peers{C_RESET}       – list discovered LXMF peers")
    info(f"  {C_BOLD}/me{C_RESET}          – show your LXMF address and name")
    info(f"  {C_BOLD}/announce{C_RESET}    – re-announce yourself")
    info(f"  {C_BOLD}/help{C_RESET}        – this help")
    info(f"  {C_BOLD}/rename <index|hash> <name>{C_RESET}  – set a persistent local name")
    info(f"  {C_BOLD}/quit{C_RESET}        – exit")
    info(f"  {C_BOLD}<text>{C_RESET}       – send to active peer")
    info("")


# ────────────────────────────────────────────────────────────────────────────
# Periodic re-announce background thread
# ────────────────────────────────────────────────────────────────────────────
def _announce_loop(interval: int = 300) -> None:
    while True:
        time.sleep(interval)
        if router and local_dest:
            try:
                router.announce(local_dest.hash)
            except Exception:
                pass


# ────────────────────────────────────────────────────────────────────────────
# Initialise RNS + LXMF
# ────────────────────────────────────────────────────────────────────────────
def init(storage_path: str, name: str, rns_config: str | None) -> None:
    global router, local_dest, local_identity, display_name

    display_name = name
    global _storage_path
    _storage_path = storage_path
    os.makedirs(storage_path, exist_ok=True)
    _load_custom_names()

    # ── Reticulum ────────────────────────────────────────────────────────────
    info("Starting Reticulum…")
    if rns_config:
        RNS.Reticulum(configdir=rns_config)
    else:
        RNS.Reticulum()

    # ── Identity (persistent) ────────────────────────────────────────────────
    id_path = os.path.join(storage_path, "identity")
    if os.path.exists(id_path):
        local_identity = RNS.Identity.from_file(id_path)
        info(f"Loaded identity from {id_path}")
    else:
        local_identity = RNS.Identity()
        local_identity.to_file(id_path)
        info(f"New identity saved to {id_path}")

    # ── LXM Router ───────────────────────────────────────────────────────────
    # Let the router create and own the lxmf.delivery destination internally.
    # Creating it ourselves first causes a duplicate-registration crash.
    router = LXMF.LXMRouter(
        identity    = local_identity,
        storagepath = storage_path,
    )
    router.register_delivery_identity(local_identity, display_name=display_name)
    router.register_delivery_callback(on_delivery)

    # Retrieve the real Destination object the router just registered.
    # We must NOT create a second one – that causes the duplicate crash.
    local_dest_hash = RNS.Destination.hash(local_identity, "lxmf", "delivery")
    local_dest = next(
        (d for d in RNS.Transport.destinations if d.hash == local_dest_hash),
        None,
    )
    if local_dest is None:
        raise RuntimeError("Could not locate our LXMF delivery destination after registration.")

    # ── Peer discovery ───────────────────────────────────────────────────────
    RNS.Transport.register_announce_handler(AnnounceHandler())

    # ── First announce ───────────────────────────────────────────────────────
    router.announce(local_dest.hash)
    info(f"Announced as {C_BOLD}{display_name}{C_RESET}  <{local_dest.hash.hex()}>")

    # ── Background re-announce ───────────────────────────────────────────────
    t = threading.Thread(target=_announce_loop, daemon=True)
    t.start()


# ────────────────────────────────────────────────────────────────────────────
# Main REPL
# ────────────────────────────────────────────────────────────────────────────
def repl() -> None:
    global active_peer, current_prompt

    info("")
    info(f"  {C_BOLD}LXMF Chat{C_RESET} – Reticulum MeshChat compatible")
    info(f"  Type {C_BOLD}/help{C_RESET} for commands, {C_BOLD}/quit{C_RESET} to exit.")
    info("")
    cmd_me()
    info("")
    # Draw the initial prompt (after this, _print handles repainting it)
    current_prompt = f"{C_DIM}[no peer]{C_RESET} "
    sys.stdout.write(current_prompt)
    sys.stdout.flush()

    while True:
        peer_label     = (_display_name_for(active_peer) or active_peer) if active_peer else "no peer"
        current_prompt = f"{C_DIM}[{peer_label}]{C_RESET} "
        try:
            line = input("").strip()
        except (EOFError, KeyboardInterrupt):
            current_prompt = ""
            print()
            info("Bye!")
            break

        if not line:
            # Nothing was printed, so draw the prompt manually
            sys.stdout.write(current_prompt)
            sys.stdout.flush()
            continue

        if line.startswith("/"):
            parts = line[1:].split(None, 1)
            cmd   = parts[0].lower()
            args  = parts[1] if len(parts) > 1 else ""

            if cmd in ("quit", "exit", "q"):
                info("Bye!")
                break
            elif cmd == "help":
                cmd_help()
            elif cmd == "peers":
                cmd_peers()
            elif cmd == "me":
                cmd_me()
            elif cmd == "announce":
                cmd_announce()
            elif cmd == "to":
                cmd_to(args)
            elif cmd == "rename":
                cmd_rename(args)
            else:
                warn(f"Unknown command /{cmd} – type /help")
        else:
            if active_peer is None:
                warn("No active peer.  Use /to <hash> or /peers to pick one.")
            else:
                send_message(active_peer, line)


# ────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ────────────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Console LXMF chat – compatible with Reticulum MeshChat",
    )
    p.add_argument(
        "--name", "-n",
        default="LXMFChat",
        help="Display name announced to the network (default: LXMFChat)",
    )
    p.add_argument(
        "--to", "-t",
        dest="peer",
        metavar="HASH",
        help="32-char LXMF address of the peer to start chatting with",
    )
    p.add_argument(
        "--storage",
        default=os.path.expanduser("~/.lxmf-chat"),
        metavar="PATH",
        help="Directory for identity and LXMF storage (default: ~/.lxmf-chat)",
    )
    p.add_argument(
        "--rns-config",
        metavar="DIR",
        help="Reticulum config directory (default: ~/.reticulum)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    init(
        storage_path = args.storage,
        name         = args.name,
        rns_config   = args.rns_config,
    )

    if args.peer:
        h = args.peer.strip().lower().replace(":", "")
        if len(h) == 32:
            global active_peer
            active_peer = h
            info(f"Active peer set from command line: {h}")
        else:
            warn("--to hash must be 64 hex characters – ignored.")

    repl()


if __name__ == "__main__":
    main()
