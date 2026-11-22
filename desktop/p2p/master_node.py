"""The shipped master-node identity — Sautium's always-on support node.

Every node auto-adds this contact at P2P start (silently; removal is
respected forever) and reaches it via the ordinary DHT user-key lookup.
The master is an edge-verified cache and a message relay, never a trusted
authority over what nodes hold (docs/design/P2P-SYNC-INTEGRITY.md) — these
constants pin its exact identity so a DHT impersonator fails the handshake
pubkey check. It authors catalogue slices like any other dump holder
(since 2026-11-07): what a slice says counts only once MusicBrainz's answers
confirm it (desktop/p2p/slice_trust.py).

The master account is deterministic (Argon2id over its username+password),
so rotating that password rotates the identity and REQUIRES shipping new
constants. Empty values mean "no master configured" — dev builds before
activation simply skip the auto-add.

MIRRORED: desktop/p2p/master_node.py <-> backend/master_node.py (the
Docker image builds from backend/ only). Update both together. The role
below is this copy's alone: one decision per process, both runtimes.
"""

from typing import Optional

MASTER_USERNAME = "Sautium"
MASTER_INVITE_CODE = "Sautium#FBE3-7BA9-57CE"
# Full 64-hex pin; the 48-bit invite hash alone is guessable.
MASTER_PUBKEY_HEX = ("3aa2ae91bc41863468ea4df3346811bc"
                     "b0f7e0d6a9644b931cfd628d81247042")
# Fixed UUID of the public support token (minted on the master with
# require_birth_cert=TRUE — the anti-sybil gate).
MASTER_TOKEN_ID = "95f7c9f1-6b74-4f98-824d-faf7a1030d3b"


def master_configured() -> bool:
    return bool(MASTER_INVITE_CODE and MASTER_PUBKEY_HEX and MASTER_TOKEN_ID)


# Whether this process is the master: decided once by its start (the
# backend's lifespan, the launcher's P2PManager.start) from the key it signs
# with, read by every path that acts as the master. A remote's pin is never
# read from here: "that peer is the master" stays a comparison with
# MASTER_PUBKEY_HEX.
_role: Optional[bool] = None


def decide(public_key_hex: Optional[str]) -> bool:
    """This process's role from its own key: the master exactly when the key
    is the pin. The start that calls it has already refused a backend meant
    to be the master under another key (p2p_identity.master_refusal)."""
    global _role
    _role = bool(public_key_hex) and public_key_hex.lower() == MASTER_PUBKEY_HEX.lower()
    return _role


def is_master() -> bool:
    if _role is None:
        raise RuntimeError("the master role is read before this process's start decided it")
    return _role
