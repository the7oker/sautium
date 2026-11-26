"""What a page may ask of the host agent, and the agent's answers
(backend/host_channel.py says the channel's form and its threats).

- POST /api/host/{verb} — any signed-in device: leaves the request. The
  confirm before an update is the page's (PI-NODE-2B); a node an attacker
  controls skips it, which is why every verb's cost is bounded (PI-NODE-1
  F2): a check or diagnostics minutes and megabytes, an update at most once
  a day and only to a verified release — its build compiles Python once per
  pin, most of an hour on a small board.
- POST /api/host/answer — the agent alone: the middleware admits the
  channel's key on this path and nowhere else, and this route takes no other
  signer. The answer is kept per verb, the page woken (the library stream's
  pattern) and whoever waits on this process woken (host_channel.answered:
  the support worker); an update that failed or was taken back is recorded
  for support as `update.failed`, the launcher's kind.
- GET /api/host — whether this node has an agent, and every verb's last
  request and answer.
- GET /api/host/diagnostics/report — the last report as text for the page,
  and when it was made.
"""

import logging
import secrets
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import host_channel
from db_pool import db_execute, db_query

logger = logging.getLogger(__name__)

# This run of the node, to a page that drew the node's state before it
_RUN = secrets.token_hex(8)

router = APIRouter(prefix="/api/host", tags=["host"])


class Answer(BaseModel):
    verb: str
    outcome: str
    detail: str = Field("", max_length=host_channel.DETAIL_MAX)
    release: Optional[str] = Field(None, max_length=host_channel.RELEASE_MAX)
    report: Optional[str] = Field(None, max_length=host_channel.REPORT_MAX)
    # An earlier run's answer the node missed, said again (scripts/host_agent.py)
    replayed: bool = False


@router.get("")
async def host_state() -> dict:
    rows = db_query("SELECT verb::text, asked_at, outcome::text, detail, release, report IS NOT NULL AS has_report, "
                    "answered_at FROM host_requests ORDER BY verb")
    return {"available": host_channel.available(),
            # Which run of the node answers: another after a restart or an
            # update, and what a page drew of the node before is then old
            "run": _RUN,
            "requests": {row.pop("verb"): row for row in rows},
            # The release a check offered while this node still runs an older one
            "offered": (offered_release() or {}).get("release")}


def offered_release() -> Optional[dict]:
    """The release the agent's last check offered, while this node still
    runs an older one: {release, since}. None once the tree states that
    version (an update that moved the node leaves the check's row as it
    was until the next check)."""
    if not host_channel.available():
        return None
    row = db_query("SELECT release, answered_at FROM host_requests "
                   "WHERE verb = 'check' AND outcome = 'offered' AND release IS NOT NULL")
    if not row:
        return None
    from desktop import release_verify, updater
    offered = release_verify.parse_version(row[0]["release"])
    if offered is None or offered <= updater.installer_version():
        return None
    return {"release": row[0]["release"], "since": row[0]["answered_at"].isoformat()}


def update_report() -> Optional[dict]:
    """How the last update asked from here ended, where it moved the node
    or was taken back: a report for the notices (said once, then dropped) —
    the page that asked lost its stream when the node stopped."""
    row = db_query("SELECT outcome::text, release, answered_at FROM host_requests "
                   "WHERE verb = 'update' AND outcome IN ('updated', 'undone') "
                   "AND answered_at IS NOT NULL")
    if not row:
        return None
    return {"outcome": row[0]["outcome"], "release": row[0]["release"],
            "since": row[0]["answered_at"].isoformat()}


# The node asks for a check itself when nobody looks: at its start and once
# a day (the agent's own floor is hourly). No timer of the host's.
CHECK_EVERY_S = 24 * 3600


def check_if_due() -> bool:
    """Ask the agent for a check unless one was asked within a day: called
    at the node's start and by the background pass it already runs."""
    if not host_channel.available():
        return False
    row = db_query("SELECT 1 FROM host_requests WHERE verb = 'check' "
                   "AND asked_at > now() - make_interval(secs => %s)", (CHECK_EVERY_S,))
    if row:
        return False
    try:
        leave_request("check")
    except (host_channel.NoChannel, OSError) as e:
        # The request's own name unusable (a folder there, a link, a
        # read-only mount): this check is not asked, and neither the node's
        # start nor the background pass that calls this is the worse for it
        logger.warning("the daily check for updates was not asked: %s", e)
        return False
    return True


@router.get("/diagnostics/report")
async def diagnostics_report() -> dict:
    row = db_query("SELECT report, answered_at FROM host_requests WHERE verb = 'diagnostics'")
    if not row or row[0]["report"] is None:
        raise HTTPException(status_code=404, detail="no diagnostics from the host yet")
    page = host_channel.report_page(row[0]["report"])
    # A floor's answer moves answered_at and keeps the report: its age is its own
    return {"report": page["report"], "answered_at": page["made_at"] or row[0]["answered_at"]}


def leave_request(verb: str) -> None:
    """The request left for the agent and on record, the page woken — a
    page's ask and the support worker's (backend/diag_node.py)."""
    host_channel.ask(verb)
    db_execute("INSERT INTO host_requests (verb, asked_at) VALUES (%s, now()) "
               "ON CONFLICT (verb) DO UPDATE SET asked_at = EXCLUDED.asked_at", (verb,))
    from routers.settings import notify_library_subscribers
    notify_library_subscribers()


@router.post("/answer")
async def answer(body: Answer, request: Request) -> dict:
    if getattr(request.state, "signer", None) != "host-agent":
        raise HTTPException(status_code=403, detail="the host agent's own")
    if body.outcome not in host_channel.OUTCOMES.get(body.verb, ()):
        raise HTTPException(status_code=422, detail=f"no outcome {body.outcome!r} for {body.verb!r}")
    db_execute(
        "INSERT INTO host_requests (verb, outcome, detail, release, report, answered_at) "
        "VALUES (%(verb)s, %(outcome)s, %(detail)s, %(release)s, %(report)s, now()) "
        "ON CONFLICT (verb) DO UPDATE SET outcome = EXCLUDED.outcome, detail = EXCLUDED.detail, "
        "release = EXCLUDED.release, answered_at = EXCLUDED.answered_at, "
        # An answer the agent sends again after the node missed it carries no
        # report: the one kept stays
        "report = CASE WHEN EXCLUDED.verb = 'diagnostics' AND EXCLUDED.outcome = 'done' "
        "AND EXCLUDED.report IS NOT NULL THEN EXCLUDED.report ELSE host_requests.report END",
        body.model_dump(exclude={"replayed"}))
    logger.info("the host agent answered %s: %s", body.verb, body.outcome)
    if body.verb == "update" and body.outcome in ("failed", "undone"):
        from db_pool import get_conn
        from desktop.p2p import diag_events
        with get_conn() as conn:
            diag_events.record(conn, "update.failed", {
                "step": "host agent", "outcome": body.outcome, "error": body.detail[:500]})
    from routers.settings import notify_library_subscribers
    notify_library_subscribers()
    if not body.replayed:
        host_channel.answered(body.verb, body.outcome)
    if body.verb in ("check", "update"):
        # A release offered, or no longer; an update's end: the notices
        # update.available and update.result
        db_execute("NOTIFY sautium_notices")
    return {"ok": True}


@router.post("/{verb}")
async def ask(verb: str) -> dict:
    if verb not in host_channel.VERBS:
        raise HTTPException(status_code=404, detail="no such request")
    try:
        leave_request(verb)
    except host_channel.NoChannel as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return {"asked": verb}
