import RNS
import sys
import time
import threading
import os

# --- CONFIGURATION ---
APP_NAME = "meshchat"
ASPECT   = "messenger"

def packet_received(data, packet):
    """Callback for when a message arrives on an active link"""
    try:
        # Trigger the system bell
        print("\a", end="", flush=True)

        text = data.decode("utf-8")
        remote_identity = packet.link.get_remote_identity()
        if remote_identity:
            remote_id = RNS.prettyhexrep(remote_identity.hash)
        else:
            remote_id = "Unknown (Identifying...)"
        
        print(f"\r[{remote_id}] >> {text}\n> ", end="", flush=True)
    except Exception as e:
        print(f"\n[!] Error decoding packet: {e}")

def link_closed(link):
    """Callback for when the connection drops"""
    print(f"\n[!] Link closed. Reason: {link.teardown_reason}")
    os._exit(0)

# --- SHARED INPUT LOOP ---
def input_loop(link):
    while True:
        try:
            message = input("> ")
            if message.strip():
                RNS.Packet(link, message.encode("utf-8")).send()
        except (EOFError, KeyboardInterrupt):
            print("\n[*] Tearing down link...")
            link.teardown()
            break

# --- SERVER LOGIC ---
def run_server():
    RNS.Reticulum()
    
    server_id = RNS.Identity.from_file("./server.id") or RNS.Identity()
    server_id.to_file("./server.id")
    
    dest = RNS.Destination(server_id, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, ASPECT)
    
    def link_established(link):
        link.identify(server_id)
        print(f"\n[*] New connection established.")
        link.set_packet_callback(packet_received)
        link.set_link_closed_callback(link_closed)
        threading.Thread(target=input_loop, args=(link,), daemon=True).start()

    dest.set_link_established_callback(link_established)
    
    # --- PRINT SERVER IDENTITY ---
    print(f"[*] Server Identity:  {RNS.prettyhexrep(server_id.hash)}")
    print(f"[*] Destination Hash: {RNS.prettyhexrep(dest.hash)}")
    print("[*] Waiting for client... (Announcing every 60s)")
    
    try:
        while True:
            dest.announce()
            time.sleep(60)
    except KeyboardInterrupt:
        sys.exit(0)

# --- CLIENT LOGIC ---
def run_client(destination_hex):
    RNS.Reticulum()
    
    try:
        target_hash = bytes.fromhex(destination_hex)
    except:
        print("[!] Invalid hex hash.")
        return

    client_id = RNS.Identity.from_file("./client.id") or RNS.Identity()
    client_id.to_file("./client.id")

    # --- PRINT CLIENT IDENTITY ---
    print(f"[*] Your Identity: {RNS.prettyhexrep(client_id.hash)}")

    if not RNS.Transport.has_path(target_hash):
        print("[*] Requesting path to server...")
        RNS.Transport.request_path(target_hash)
        while not RNS.Transport.has_path(target_hash): 
            time.sleep(1)

    server_identity = RNS.Identity.recall(target_hash)
    dest = RNS.Destination(server_identity, RNS.Destination.OUT, RNS.Destination.SINGLE, APP_NAME, ASPECT)

    print("[*] Establishing encrypted link...")
    link = RNS.Link(dest)
    link.set_packet_callback(packet_received)
    link.set_link_closed_callback(link_closed)

    while link.status != RNS.Link.ACTIVE:
        time.sleep(0.1)

    link.identify(client_id)
    print("[*] Connected! Type your message below:")
    input_loop(link)

if __name__ == "__main__":
    if len(sys.argv) == 1:
        run_server()
    elif len(sys.argv) == 2:
        run_client(sys.argv[1])
    else:
        print("Usage: \n Server: python3 meshchat.py \n Client: python3 meshchat.py <hash>")
