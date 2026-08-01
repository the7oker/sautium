"""The shipped master-node identity — Sautium's always-on support node.

Every node auto-adds this contact at P2P start (silently; removal is
respected forever) and reaches it via the ordinary DHT user-key lookup.
The master is an edge-verified cache and a message relay, never a trusted
authority (docs/design/P2P-SYNC-INTEGRITY.md) — these constants pin its
exact identity so a DHT impersonator fails the handshake pubkey check.

The master account is deterministic (Argon2id over its username+password),
so rotating that password rotates the identity and REQUIRES shipping new
constants. Empty values mean "no master configured" — dev builds before
activation simply skip the auto-add.

MIRRORED: backend/master_node.py <-> desktop/p2p/master_node.py (the
Docker image builds from backend/ only, the launcher build cannot import
backend modules). Update both together.
"""

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
