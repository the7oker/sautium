"""
EQ Preset API endpoints.

Serves generated EQ preset files for download.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from eq_generator import EQ_OUTPUT_DIR, list_eq_presets

router = APIRouter(prefix="/api/eq", tags=["eq"])


@router.get("/presets")
async def get_presets():
    """List all generated EQ presets."""
    return list_eq_presets()


@router.get("/download/{filename}")
async def download_preset(filename: str):
    """Download a generated EQ preset file."""
    # Prevent path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    filepath = EQ_OUTPUT_DIR / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail=f"Preset '{filename}' not found")

    return FileResponse(
        path=str(filepath),
        filename=filename,
        media_type="text/plain",
    )
