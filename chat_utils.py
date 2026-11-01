"""Small safety and terminal helpers shared by the two console clients."""

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sys
import unicodedata

import RNS


def safe_text(value: object, *, multiline: bool = False) -> str:
    """Render control characters visibly instead of letting a peer control the terminal."""
    result = []
    for char in str(value):
        if char.isprintable() or (multiline and char in "\n\t"):
            result.append(char)
        elif unicodedata.category(char) == "Cf" or ord(char) > 255:
            result.append(f"\\u{ord(char):04x}")
        else:
            result.append(f"\\x{ord(char):02x}")
    return "".join(result)


def normalize_address(value: str) -> str:
    """Accept a 16-byte Reticulum destination hash, optionally colon-separated."""
    value = value.strip().lower().replace(":", "")
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("Address must be 32 hexadecimal characters")
    return value


def address_argument(value: str) -> str:
    """Preserve the useful validation message in argparse's CLI error output."""
    try:
        return normalize_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def load_identity(path: str | Path) -> RNS.Identity:
    """Never silently replace a corrupt identity, or create a world-readable key."""
    path = Path(path)
    if path.exists():
        identity = RNS.Identity.from_file(str(path))
        if identity is None:
            raise ValueError(f"Cannot load identity from {path}; restore it from a backup")
        path.chmod(0o600)
        return identity

    identity = RNS.Identity()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as key_file:
        key_file.write(identity.get_private_key())
    return identity


@contextmanager
def console_input(prompt):
    """Keep typed text intact when network callbacks print during interactive input."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import ANSI
        from prompt_toolkit.patch_stdout import patch_stdout

        session = PromptSession()
        with patch_stdout():
            yield lambda: session.prompt(lambda: ANSI(prompt()))
    else:
        yield lambda: input(prompt())
