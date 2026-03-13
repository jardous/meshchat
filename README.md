# P2P RNS console chat 🛰️

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
