"""Cover art proxy endpoint.

Serves pre-encoded WebP blobs from the `covers` table with long-lived cache
headers. URLs include a `v` timestamp query parameter for cache-busting when
the underlying source file is re-imported.
"""

import logging

from fastapi import APIRouter, HTTPException, Path as FPath
from fastapi.responses import Response
from sqlalchemy import text

from database import get_db_context

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/covers", tags=["covers"])


@router.get("/{cover_id}")
async def get_cover(cover_id: str = FPath(..., min_length=36, max_length=36)):
    """Return WebP cover bytes with `immutable` cache headers."""
    try:
        with get_db_context() as db:
            row = db.execute(
                text(
                    "SELECT data, content_hash FROM covers WHERE id = :id"
                ),
                {"id": cover_id},
            ).first()
    except Exception as e:
        logger.error(f"cover lookup failed for {cover_id}: {e}")
        raise HTTPException(status_code=500, detail="cover lookup failed")

    if not row:
        raise HTTPException(status_code=404, detail="cover not found")

    data, content_hash = row
    etag = f'"{content_hash.hex()[:16]}"'

    return Response(
        content=bytes(data),
        media_type="image/webp",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": etag,
        },
    )
