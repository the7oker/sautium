"""The Web UI's "Back up now" (backend/backup_job.py): the making only.

- POST /api/backup — the account's password, through the credential box
  (routers/auth.boxed) and the signed door's count (step_up): a device token
  alone starts nothing, and a wrong password is counted as at any door.
- GET /api/backup — the job's state and the folder's files.
- POST /api/backup/cancel, POST /api/backup/keep-newest — the latter the
  owner's confirmed removal of all but the newest few.

No route of this app serves a backup file: it holds the node's identity
under the password's strength alone, and never crosses the LAN by HTTP (the
owner's answer of 2026-11-15). A test holds the whole app to that.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

import backup_job
from routers.auth import StepUp, boxed, step_up

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("")
def get_backup() -> Dict[str, Any]:
    return backup_job.state()


@router.post("")
async def post_backup(opened=boxed(StepUp)) -> Dict[str, Any]:
    """Start a backup, on the account's password. An account whose password
    nobody knows has no key to give a backup: the job itself says so."""
    req, reply = opened
    await step_up(req)
    if not req.password:
        # The host's pairing code vouched (an anonymous account): there is
        # no password to key the file on
        raise HTTPException(status_code=409, detail="This account has no password: set one in "
                                                    "Profile, then back up")
    if not backup_job.start(req.password):
        raise HTTPException(status_code=409, detail="A backup is running")
    return reply(backup_job.state())


@router.post("/cancel")
def post_cancel() -> Dict[str, Any]:
    backup_job.cancel()
    return backup_job.state()


@router.post("/keep-newest")
def post_keep_newest() -> Dict[str, Any]:
    try:
        result = backup_job.keep_newest()
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {**result, **backup_job.state()}
