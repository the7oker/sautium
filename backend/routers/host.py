"""What a page may ask of the host agent, and the agent's answers
(backend/host_channel.py says the channel's form and its threats).

- POST /api/host/{verb} — any signed-in device: leaves the request. The
  confirm before an update is the page's (PI-NODE-2B); a node an attacker
  controls skips it, which is why every verb costs at most minutes and
  megabytes (PI-NODE-1 F2).
- POST /api/host/answer — the agent alone: the middleware admits the
  channel's key on this path and nowhere else, and this route takes no other
  signer. The answer is kept per verb and the page woken (the library
  stream's pattern).
- GET /api/host — whether this node has an agent, and every verb's last
  request and answer.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import host_channel
from db_pool import db_execute, db_query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/host", tags=["host"])


class Answer(BaseModel):
    verb: str
    outcome: str
    detail: str = Field("", max_length=host_channel.DETAIL_MAX)
    release: Optional[str] = Field(None, max_length=host_channel.RELEASE_MAX)
    report: Optional[str] = Field(None, max_length=host_channel.REPORT_MAX)


@router.get("")
async def host_state() -> dict:
    rows = db_query("SELECT verb::text, asked_at, outcome::text, detail, release, report IS NOT NULL AS has_report, "
                    "answered_at FROM host_requests ORDER BY verb")
    return {"available": host_channel.available(),
            "requests": {row.pop("verb"): row for row in rows}}


@router.get("/diagnostics/report")
async def diagnostics_report() -> dict:
    row = db_query("SELECT report, answered_at FROM host_requests WHERE verb = 'diagnostics'")
    if not row or row[0]["report"] is None:
        raise HTTPException(status_code=404, detail="no diagnostics from the host yet")
    return row[0]


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
        "report = CASE WHEN EXCLUDED.verb = 'diagnostics' AND EXCLUDED.outcome = 'done' "
        "THEN EXCLUDED.report ELSE host_requests.report END",
        body.model_dump())
    logger.info("the host agent answered %s: %s", body.verb, body.outcome)
    from routers.settings import notify_library_subscribers
    notify_library_subscribers()
    return {"ok": True}


@router.post("/{verb}")
async def ask(verb: str) -> dict:
    if verb not in host_channel.VERBS:
        raise HTTPException(status_code=404, detail="no such request")
    try:
        host_channel.ask(verb)
    except host_channel.NoChannel as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    db_execute("INSERT INTO host_requests (verb, asked_at) VALUES (%s, now()) "
               "ON CONFLICT (verb) DO UPDATE SET asked_at = EXCLUDED.asked_at", (verb,))
    from routers.settings import notify_library_subscribers
    notify_library_subscribers()
    return {"asked": verb}
