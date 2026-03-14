# A very simple P2P RNS console chat

A lightweight, decentralized, and end-to-end encrypted chat application built on the Reticulum Network Stack. 

MeshChat allows for private communication without a central server, internet connection, or cellular infrastructure.

## Features
* **Decentralized:** No central server; peer-to-peer communication.
* **End-to-End Encryption:** Secured via asymmetric cryptography.
* **Identity Persistence:** Your identity is tied to local .id files.
* **Automatic Discovery:** Finds paths via node announcements.

## Prerequisites
- Python 3.9 or higher
- Reticulum Network Stack (rns)

`pip install rns`

## Usage

### 1. Start the Server
Run the script without arguments to act as the host.
`python3 meshchat.py`

### 2. Start the Client
Run the script with the server's hash.
`python3 meshchat.py <SERVER_HASH>`


# lxmf_chat

A minimal console chat client for the [Reticulum](https://reticulum.network) network using the LXMF protocol. Compatible with [Reticulum MeshChat](https://github.com/liamcottle/reticulum-meshchat), [Sideband](https://github.com/markqvist/Sideband), and [Nomad Network](https://github.com/markqvist/NomadNet).

## Requirements

Python 3.10+ and a working Reticulum installation (i.e. `~/.reticulum/config` already set up).

```
pip install rns lxmf
```

## Usage

```
python lxmf_chat.py [--name NAME] [--to HASH] [--storage PATH] [--rns-config DIR]
```

| Argument | Description | Default |
|---|---|---|
| `--name`, `-n` | Display name announced to the network | `LXMFChat` |
| `--to`, `-t` | 32-char LXMF address to start chatting with | — |
| `--storage` | Directory for identity and LXMF storage | `~/.lxmf-chat` |
| `--rns-config` | Reticulum config directory | `~/.reticulum` |

### Examples

```bash
# Start with a display name
python lxmf_chat.py --name "Alice"

# Start with a display name and a known peer
python lxmf_chat.py --name "Alice" --to cfbaf79db7bb43965b4b425a4f096464
```

## Commands

| Command | Description |
|---|---|
| `/to <hash>` | Set the active conversation peer (32-char LXMF address) |
| `/peers` | List all discovered LXMF peers |
| `/me` | Show your LXMF address and display name |
| `/announce` | Re-announce yourself to the network |
| `/help` | Show command list |
| `/quit` | Exit |
| anything else | Send as a message to the active peer |

## How it works

On startup the app creates a persistent RNS identity stored in `~/.lxmf-chat/identity`. The same identity (and therefore the same LXMF address) is reused across restarts.

It then announces itself on the Reticulum network with your display name. Any LXMF client that hears the announce will add you to their contacts. Peers are discovered the same way — when another client announces, their name and address are recorded and shown in `/peers`.

Messages are sent directly (DIRECT method) with automatic fallback to a propagation node if the peer is not currently reachable. The app re-announces itself every 5 minutes to stay visible on the network.

## Identity and storage

```
~/.lxmf-chat/
├── identity       # your persistent RNS key pair
└── ...            # LXMF router scratch storage
```

To start fresh with a new identity, delete `~/.lxmf-chat/identity`. Your LXMF address will change.

