"""The Web UI's "Back up now" (backend/backup_job.py): the making only.

- POST /api/backup — start the job. Nothing to type: the file is keyed on
  the node's own seed (SEC-S5), and the request is signed like every other
  (§14 Q9: no step-up — the job is single-flight, the file stays on the
  node).
- GET /api/backup — the job's state and the folder's files.
- POST /api/backup/cancel, POST /api/backup/keep-newest — the latter the
  owner's confirmed removal of all but the newest few.

No route of this app serves a backup file: it holds the node's whole
database, and never crosses the LAN by HTTP (the owner's answer of
2026-11-15). A test holds the whole app to that.
"""

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

import backup_job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("")
def get_backup() -> Dict[str, Any]:
    return backup_job.state()


@router.post("")
def post_backup() -> Dict[str, Any]:
    if not backup_job.start():
        raise HTTPException(status_code=409, detail="A backup is running")
    return backup_job.state()


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
