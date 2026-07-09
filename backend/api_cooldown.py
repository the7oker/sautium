"""Persistent, cross-source rate-limit cooldown for external APIs.

When Last.fm (API error 29) or Deezer (HTTP 429 / quota) rate-limits us,
we stop hitting that source until a deadline — and we PERSIST the
deadline so a backend restart doesn't resume hammering an API that is
still banning us. Replaces the old in-memory-only 120s photo cooldown.

Design:
  - `arm(source, reason)` records a cooldown with exponential backoff
    (30 min → doubling → 24 h ceiling). Consecutive strikes without an
    intervening success escalate; `clear()` on a later success resets
    them, so an isolated hiccup stays short while a source that keeps
    banning us backs off harder.
  - `cooling_down(source)` is the hot-path guard — a pure in-memory dict
    read (no DB round-trip per request/poll). The cache is warmed from
    the table at startup via `load_from_db()`.
  - State lives in `external_api_cooldown` (one row per source), so it
    survives restarts. Single-process backend → a module dict is a
    sufficient cache (same assumption as the rest of the app).

Sources are free-form; current callers use 'lastfm' and 'deezer'.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from db_pool import db_query, db_query_one, db_execute

logger = logging.getLogger(__name__)

_BASE_SEC = 30 * 60          # first strike: 30 minutes
_CAP_SEC = 24 * 60 * 60      # ceiling: 24 hours

# source -> UTC deadline. Hot-path cache; the table is the source of truth.
_cache: dict[str, datetime] = {}


@dataclass
class CooldownState:
    """Full known state of a source's rate-limit history — richer than the
    binary cooling_down() guard. Each consumer applies its OWN policy from it:
    enrichment just pauses while `active`, but a latency-sensitive consumer
    (e.g. streaming) can read `strikes` / `seconds_since_last_strike` to judge
    whether a source is chronically unstable and route around it in its
    fallback waterfall — even between cooldown windows."""
    source: str
    strikes: int                 # consecutive bans without an intervening success
    last_strike_at: datetime     # when arm() last fired
    cooldown_until: datetime     # deadline (may be in the past → expired, not yet cleared)
    reason: str

    @property
    def active(self) -> bool:
        """True while the cooldown window has not elapsed."""
        return self.cooldown_until > datetime.now(timezone.utc)

    @property
    def seconds_since_last_strike(self) -> float:
        return (datetime.now(timezone.utc) - self.last_strike_at).total_seconds()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def cooling_down(source: str) -> Optional[datetime]:
    """Return the active cooldown deadline for `source`, or None if free.

    Pure in-memory read — safe to call on every request / poll tick."""
    deadline = _cache.get(source)
    if deadline is not None and deadline > _now():
        return deadline
    return None


def status(source: str) -> Optional[CooldownState]:
    """The full persisted state for a source (read from the table, so it
    survives even after the cooldown window lapses — the row lingers until a
    success clears it). Returns None only when the source has never been armed
    or a later success cleared it.

    Use this — not cooling_down() — when a caller needs the strike history to
    make its own routing decision (e.g. streaming skipping a chronically-banned
    provider in its fallback waterfall). cooling_down() stays the cheap
    in-memory guard for enrichment's simple pause-while-active behavior."""
    row = db_query_one(
        "SELECT source, strikes, updated_at, cooldown_until, reason "
        "FROM external_api_cooldown WHERE source = %(s)s",
        {"s": source},
    )
    if not row:
        return None
    return CooldownState(
        source=row["source"],
        strikes=row["strikes"],
        last_strike_at=row["updated_at"],
        cooldown_until=row["cooldown_until"],
        reason=row["reason"] or "",
    )


def arm(source: str, reason: str = "") -> datetime:
    """Record a rate-limit hit and return the new cooldown deadline.

    Backoff is exponential in consecutive strikes: 30m, 1h, 2h, … capped
    at 24h. The strike count persists in the row; `clear()` resets it, so
    repeated bans without an intervening success back off progressively."""
    prev = db_query_one(
        "SELECT strikes FROM external_api_cooldown WHERE source = %(s)s",
        {"s": source},
    )
    strikes = (prev["strikes"] + 1) if prev else 1
    dur = min(_BASE_SEC * (2 ** (strikes - 1)), _CAP_SEC)
    deadline = _now() + timedelta(seconds=dur)
    db_execute(
        """
        INSERT INTO external_api_cooldown (source, cooldown_until, reason, strikes, updated_at)
        VALUES (%(s)s, %(until)s, %(r)s, %(k)s, now())
        ON CONFLICT (source) DO UPDATE SET
            cooldown_until = EXCLUDED.cooldown_until,
            reason         = EXCLUDED.reason,
            strikes        = EXCLUDED.strikes,
            updated_at     = now()
        """,
        {"s": source, "until": deadline, "r": (reason or "")[:300], "k": strikes},
    )
    _cache[source] = deadline
    logger.warning(
        "%s rate-limited (strike %d) — cooling down %s, until %s UTC: %s",
        source, strikes, _fmt_dur(dur), deadline.strftime("%Y-%m-%d %H:%M"), reason,
    )
    return deadline


def clear(source: str) -> None:
    """Reset a source's cooldown after a successful call. No-op if the
    source was never armed (pure dict check → cheap on the hot path)."""
    if _cache.pop(source, None) is not None:
        db_execute("DELETE FROM external_api_cooldown WHERE source = %(s)s", {"s": source})


def load_from_db() -> None:
    """Warm the in-memory cache from the persisted table at startup so a
    restart mid-cooldown doesn't resume hitting a still-banning source."""
    rows = db_query(
        "SELECT source, cooldown_until FROM external_api_cooldown WHERE cooldown_until > now()"
    )
    for row in rows:
        _cache[row["source"]] = row["cooldown_until"]
    if rows:
        logger.info(
            "Loaded %d active API cooldown(s) from DB: %s",
            len(rows), ", ".join(r["source"] for r in rows),
        )


def _fmt_dur(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"
