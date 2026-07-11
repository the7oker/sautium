"""
Media byte routes for the browser output (<audio> src).

Same HTTPS origin as the app — no mixed content, no extra listening port.
Authenticated by short-lived query-param signatures (media_urls) because
audio elements can't set the HMAC headers; the auth_hmac middleware
whitelists the prefix for that reason. Range is fully supported — seek
and Safari's probing both depend on it.
"""

import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

import media_urls
from config import settings
from db_pool import db_query_one

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/player/media", tags=["media"])

_MIME_BY_FORMAT = {
    "FLAC": "audio/flac", "MP3": "audio/mpeg", "WAV": "audio/wav",
    "OGG": "audio/ogg", "M4A": "audio/mp4", "AIFF": "audio/aiff",
}
_CHUNK = 262144


def _parse_range(request: Request, total: int) -> Optional[tuple[int, int]]:
    rng = request.headers.get("range")
    if not (rng and rng.startswith("bytes=")):
        return None
    try:
        s, _, e = rng[len("bytes="):].partition("-")
        start = int(s) if s else 0
        end = min(int(e), total - 1) if e else total - 1
        if 0 <= start <= end:
            return start, end
    except ValueError:
        pass
    return None


def _range_headers(start: int, end: int, total: int, mime: str) -> dict:
    return {
        "Content-Type": mime,
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        "Content-Range": f"bytes {start}-{end}/{total}",
    }


@router.get("/file/{media_file_id}")
def media_file(media_file_id: int, request: Request,
               exp: Optional[str] = None, sig: Optional[str] = None):
    """Owned file bytes for the browser <audio> element."""
    if not media_urls.verify("file", str(media_file_id), exp, sig):
        raise HTTPException(status_code=401, detail="invalid or expired media signature")

    row = db_query_one(
        "SELECT file_path, file_format FROM media_files WHERE id = %(id)s",
        {"id": media_file_id})
    if not row:
        raise HTTPException(status_code=404, detail="media file not found")
    path = settings.translate_to_local_path(row["file_path"])
    try:
        total = os.path.getsize(path)
    except OSError:
        raise HTTPException(status_code=404, detail="file missing on disk")
    mime = _MIME_BY_FORMAT.get((row["file_format"] or "").upper(),
                               "application/octet-stream")

    span = _parse_range(request, total)
    start, end = span if span else (0, total - 1)

    def reader():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    if span:
        return StreamingResponse(reader(), status_code=206,
                                 headers=_range_headers(start, end, total, mime))
    return StreamingResponse(reader(), status_code=200, headers={
        "Content-Type": mime,
        "Accept-Ranges": "bytes",
        "Content-Length": str(total),
    })


@router.get("/preview/{token}")
def media_preview(token: str, request: Request,
                  exp: Optional[str] = None, sig: Optional[str] = None):
    """Phantom-stream bytes re-served through the HTTPS origin — the browser
    can't fetch the plain-http LAN proxy from an https page (mixed content).
    Reads the proxy's existing in-memory buffer, not a second fetch."""
    if not media_urls.verify("preview", token, exp, sig):
        raise HTTPException(status_code=401, detail="invalid or expired media signature")

    from streaming import service as streaming_service
    proxy = streaming_service.get_proxy()
    entry = proxy.entry_bytes(token) if proxy else None
    if entry is None:
        raise HTTPException(status_code=404, detail="preview buffer not available")
    data, mime = entry
    total = len(data)

    span = _parse_range(request, total)
    if span:
        start, end = span
        return Response(data[start:end + 1], status_code=206,
                        headers=_range_headers(start, end, total, mime))
    return Response(data, status_code=200, headers={
        "Content-Type": mime,
        "Accept-Ranges": "bytes",
        "Content-Length": str(total),
    })
