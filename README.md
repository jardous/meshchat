# MeshChat console

[![Tests](https://github.com/jardous/meshchat/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/jardous/meshchat/actions/workflows/tests.yml)
[![Build](https://github.com/jardous/meshchat/actions/workflows/build.yml/badge.svg?branch=main)](https://github.com/jardous/meshchat/actions/workflows/build.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Two small console chat clients for [Reticulum](https://reticulum.network):

- **`lxmf_chat.py` / `lxmf-chat`**: the recommended client, using LXMF for interoperability
  with [Reticulum MeshChat](https://github.com/liamcottle/reticulum-meshchat),
  [Sideband](https://github.com/markqvist/Sideband), and [Nomad Network](https://github.com/markqvist/NomadNet).
- **`meshchat.py` / `meshchat`**: a minimal live-link chat demo using its own protocol.
  It does **not** exchange messages with LXMF clients.

Neither application needs a central chat server. Connectivity comes from your
Reticulum interfaces: local networking, LoRa, TCP, I2P, or other supported media.
Configure Reticulum separately; these clients do not modify its configuration.

## Installation

Requires **Python 3.10+** and a working Reticulum configuration. From this checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

This installs Reticulum, LXMF, and prompt-toolkit. The latter keeps unfinished
input intact when messages arrive. `feh` is an optional **system package** used
only by `/open` to view received images; nothing is opened automatically.

## LXMF client

```bash
lxmf-chat --name "Alice"
lxmf-chat --name "Alice" --to <32-character-LXMF-address>

# Alternatively, run directly from this checkout after installing dependencies:
python lxmf_chat.py --name "Alice"
```

Select a recipient with `/to <address>` or `/peers` followed by `/to <index>`.
Incoming messages never change your selected recipient, including when no peer
is selected. Announced display names are public labels, not proof of someone's
real-world identity; verify and exchange addresses through a trusted channel.

### Options

| Option | Purpose | Default |
|---|---|---|
| `--name`, `-n` | Public display name | `LXMFChat` |
| `--to`, `-t` | Initial recipient, 32 hexadecimal characters | None |
| `--storage` | Identity, contacts, and attachment storage | `~/.lxmf-chat` |
| `--rns-config` | Reticulum configuration directory | Reticulum's default |
| `--propagation-node` | LXMF propagation destination address | None |
| `--announce-interval` | Seconds between periodic announces; `0` means startup/manual only | `3600` |
| `--path-timeout` | Seconds to wait for an unknown recipient identity | `30` |
| `--max-attachment-kib` | Maximum size of a saved attachment | `1024` |
| `--attachment-quota-mib` | Total attachment storage budget | `50` |

Addresses may also be colon-separated. Announce intervals must be `0` or at least
600 seconds. Periodic announces have jitter, and manual announces have a
10-minute cooldown. A startup announce is always sent, even with interval `0`.

### Commands

| Command | Purpose |
|---|---|
| `/to <index\|address>` | Select the recipient |
| `/peers` | List discovered and saved peers; indexes refer to the last listing |
| `/rename <index\|address> <name>` | Assign a persistent local name |
| `/save` | Save the active peer across restarts |
| `/me` | Show your LXMF address and public display name |
| `/announce` | Announce, subject to cooldown |
| `/open <index>` | Open a received image with `feh` |
| `/status` | Show outgoing delivery states and propagation-sync progress |
| `/sync` | Fetch messages from the configured propagation node |
| `/help` | Show commands |
| `/quit`, `/exit` | Exit |
| Other text | Queue a message for the active peer |

### Delivery and offline messages

Messages are **queued**, not immediately reported as delivered. The client
requests unknown recipient identities in the background with a bounded timeout,
so the prompt remains responsive. `/status` reports `queued`, `sending`,
`delivered`, `propagating`, `propagated`, `failed`, `rejected`, or `cancelled`.

To enable fallback after a direct-delivery failure, explicitly configure an
LXMF **propagation address**, not a transport identity or TCP hostname:

```bash
lxmf-chat --name "Alice" --propagation-node <32-character-propagation-address>
```

The client retries via that node once. **`propagated` means accepted by the
propagation node, not received by the final recipient.** The recipient must
retrieve stored messages through a suitable propagation node; `/sync` performs
this explicitly for this client's configured node. There is no automatic
propagation-node selection or periodic synchronization.

An unknown recipient identity is still needed to encrypt a propagated message.
If it cannot be discovered within the timeout, verify the address and retry
after the recipient announces. Outgoing messages and their displayed statuses
are **in-memory only**: quitting does not persist pending messages for retry.

### Storage and security

```text
~/.lxmf-chat/
├── identity          # Private keys; back this up securely
├── peer_names.json   # Local aliases
├── saved_peers.json  # Saved contacts
├── attachments/      # Unique, private received files
└── lxmf/             # LXMF router state
```

- Existing identities are preserved. A corrupt identity causes an error rather
  than silently changing your address. New identity files use mode `0600`;
  the storage directory uses `0700` on POSIX systems.
- Only signature-validated incoming messages are displayed or saved. Messages
  with invalid signatures or unknown sender identities are ignored with a warning.
  If a legitimate sender's identity is unknown, wait for their announce and ask
  them to resend. Encryption and signatures do **not** establish real-world trust.
- Peer-supplied terminal control characters are rendered visibly, not executed.
- Attachments have unique filenames, per-file limits, and a total storage budget
  (accounting for at least 4 KiB per file). At most 20 attachments are saved from
  one message. Delete old attachments manually when the budget is full. LXMF's
  own incoming-transfer limit may be smaller than the configured save limit.
- `/open` invokes `feh` without a shell. Images are still untrusted files: keep
  your viewer updated. No files are executed or opened automatically.
- Contacts and attachments are **not encrypted at rest**. Use disk encryption
  where needed. This application does not keep a persistent text-message history.

Announcements expose your address and display name to the reachable Reticulum
network. A TCP interface can expose your IP to its peers; using I2P elsewhere
does not anonymize an existing direct TCP connection.

## Raw Reticulum-link demo

```bash
# Listener; prints the destination hash to give to the client:
meshchat

# Client:
meshchat <server-destination-hash>
```

Or use `python meshchat.py` and `python meshchat.py <hash>` from the checkout.

The listener uses one input loop for all connections. After a client connects
and identifies itself, use `/peers` and `/to <index>` on the listener to select
it. Messages are sent to that peer, not broadcast. `/quit` exits locally; a
remote disconnect removes only that session and does not stop the listener.

`--rns-config DIR`, `--identity FILE`, `--timeout SECONDS` (default `30`), and
`--announce-interval SECONDS` (default `3600`, minimum `600`, or `0`) are available.
The listener also supports `/announce`. Server/client keys remain in `server.id`
and `client.id` by default for compatibility; both are excluded from Git.

The raw demo uses identified, encrypted links but has no contact allowlist,
offline delivery, history, or multi-packet text transfer. It rejects messages
larger than the link packet limit; use the LXMF client for longer messages.

## Development and build

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest -q
python -m build
```

CI lints and tests on Python 3.10–3.14, including real LXMF delivery and raw-chat
reconnect tests on isolated loopback interfaces (no public peers). A separate build workflow creates source
and wheel distributions, installs the wheel, smoke-tests its console commands,
and uploads the distributions as workflow artifacts. The README badges show
the actual workflow status once these changes have been pushed to GitHub.
