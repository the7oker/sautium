"""Pure-logic tests for the shared launcher/backend modules.

Anything that touches the database or the peer protocol is NOT tested here —
those run as `--selftest` CLIs against the live Docker Postgres and as
two-node checks (launcher stand <-> Docker), per CLAUDE.md "Testing
Expectations". This tree covers primitives only: proof formats, signature
canonicalisation, deterministic hashing.

Run from the repo root: `python -m pytest tests/`.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
