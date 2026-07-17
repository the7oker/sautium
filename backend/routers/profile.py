"""
Profile endpoints — own profile (identity + audio chain) and a
read-only view of another peer's public profile.

Sautium is a single-user appliance per launcher install. Own profile
lives in the one-row user_profile table, own gear in user_gear with a
canonical gear_models / gear_brands catalog. Specs, technologies and
sentiment are normalised tables that the research worker populates;
this router just stitches them together for the UI.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from covers import ingest_cover
from database import get_db_context
from db_pool import db_execute, db_query, db_query_one
from uuid_utils import gear_brand_uuid, gear_model_uuid


router = APIRouter(prefix="/api/profile", tags=["profile"])


# ============================================================
# Request models
# ============================================================

class ProfileUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=128)
    city:         Optional[str] = Field(default=None, max_length=128)
    country:      Optional[str] = Field(default=None, min_length=2, max_length=2)
    bio:          Optional[str] = Field(default=None, max_length=2000)
    public_gear:  Optional[bool] = None
    open_to_meet: Optional[bool] = None


class GearAddRequest(BaseModel):
    # Either model_id (already canonical) or brand + model + category
    # (silent input — auto-creates brand and model rows).
    model_id: Optional[str] = None
    brand:    Optional[str] = Field(default=None, max_length=200)
    model:    Optional[str] = Field(default=None, max_length=300)
    category: str
    status:   str = Field(default="own")
    notes:    Optional[str] = Field(default=None, max_length=4000)


class GearUpdate(BaseModel):
    status: Optional[str] = None
    notes:  Optional[str] = Field(default=None, max_length=4000)


VALID_CATEGORIES = {"headphones", "iems", "dac", "amp", "player", "streamer", "power", "cable",
                    "speakers", "power_amp", "preamp", "integrated_amp",
                    "turntable", "cartridge", "phono_stage"}
VALID_STATUSES = {"own", "want", "sell", "previously_owned"}


# ============================================================
# Helpers
# ============================================================

def _none_if_blank(s: Optional[str]) -> Optional[str]:
    """Treat user-submitted '' as 'unset' — NULL is the semantic in DB."""
    if s is None:
        return None
    s = s.strip()
    return s if s else None


def _load_profile_row() -> Dict[str, Any]:
    row = db_query_one("""
        SELECT display_name, city, country, bio,
               avatar_cover_id::text AS avatar_cover_id,
               public_gear, open_to_meet, updated_at
        FROM user_profile WHERE id = 1
    """)
    if row is None:
        db_execute("INSERT INTO user_profile (id) VALUES (1) ON CONFLICT DO NOTHING")
        row = db_query_one("""
            SELECT display_name, city, country, bio,
                   avatar_cover_id::text AS avatar_cover_id,
                   public_gear, open_to_meet, updated_at
            FROM user_profile WHERE id = 1
        """)
    return row


def _load_gear_rows() -> List[Dict[str, Any]]:
    """Load the audio-chain list with brand name + a compact spec map.

    Spec rows are merged into a `specs` dict on each gear item so the
    UI doesn't have to handle a flat (key, label, value) array. The
    detail sheet hits gear_specs directly via /api/gear-models/:id for
    the full attribute metadata (units, descriptions)."""
    rows = db_query("""
        SELECT ug.id::text                AS id,
               ug.gear_model_id::text     AS gear_model_id,
               ug.status::text            AS status,
               ug.notes,
               ug.added_at,
               ug.updated_at,
               b.name                     AS brand,
               gm.model,
               gm.category,
               gm.research_state::text    AS research_state,
               gm.research_summary,
               gm.researched_at,
               gm.sentiment_score,
               gm.sentiment_sample_size
        FROM user_gear ug
        JOIN gear_models gm  ON gm.id = ug.gear_model_id
        JOIN gear_brands b   ON b.id  = gm.brand_id
        WHERE ug.removed_at IS NULL
        ORDER BY ug.added_at DESC
    """)
    if not rows:
        return []

    model_ids = [r["gear_model_id"] for r in rows]
    spec_rows = db_query("""
        SELECT gs.gear_model_id::text AS gear_model_id,
               a.key, a.label, a.unit, a.value_type, gs.value_text
        FROM gear_specs gs
        JOIN gear_spec_attributes a ON a.id = gs.attribute_id
        WHERE gs.gear_model_id = ANY(%(ids)s::uuid[])
    """, {"ids": model_ids})
    specs_by_model: Dict[str, Dict[str, Any]] = {}
    for sr in spec_rows:
        specs_by_model.setdefault(sr["gear_model_id"], {})[sr["key"]] = sr["value_text"]
    for r in rows:
        r["specs"] = specs_by_model.get(r["gear_model_id"], {})
    return rows


def _resolve_brand_id(brand_name: str) -> str:
    """Resolve-or-create a gear_brands row, return its id as text."""
    name = brand_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="brand is empty")
    brand_id = str(gear_brand_uuid(name))
    db_execute(
        """
        INSERT INTO gear_brands (id, name)
        VALUES (%(id)s::uuid, %(name)s)
        ON CONFLICT (id) DO NOTHING
        """,
        {"id": brand_id, "name": name},
    )
    return brand_id


# ============================================================
# Own profile
# ============================================================

@router.get("")
def get_profile() -> Dict[str, Any]:
    profile = _load_profile_row()
    profile["gear"] = _load_gear_rows()
    return profile


@router.put("")
def update_profile(req: ProfileUpdate) -> Dict[str, Any]:
    fields: List[str] = []
    params: Dict[str, Any] = {}
    if req.display_name is not None:
        fields.append("display_name = %(display_name)s")
        params["display_name"] = _none_if_blank(req.display_name)
    if req.city is not None:
        fields.append("city = %(city)s")
        params["city"] = _none_if_blank(req.city)
    if req.country is not None:
        c = (req.country or "").strip().upper()
        fields.append("country = %(country)s")
        params["country"] = c or None
    if req.bio is not None:
        fields.append("bio = %(bio)s")
        params["bio"] = _none_if_blank(req.bio)
    if req.public_gear is not None:
        fields.append("public_gear = %(public_gear)s")
        params["public_gear"] = req.public_gear
    if req.open_to_meet is not None:
        fields.append("open_to_meet = %(open_to_meet)s")
        params["open_to_meet"] = req.open_to_meet

    if fields:
        db_execute(
            f"UPDATE user_profile SET {', '.join(fields)} WHERE id = 1",
            params,
        )
    return _load_profile_row()


# ============================================================
# Avatar upload
# ============================================================

# Image upload ceiling. WebP re-encode shrinks most photos to under
# 100 KB, but we accept raw bytes up to 10 MB so phone-camera shots
# don't fail before the encoder gets a chance.
_AVATAR_MAX_BYTES = 10 * 1024 * 1024


@router.post("/avatar")
async def upload_avatar(request: Request) -> Dict[str, Any]:
    # Raw-body upload (Content-Type: image/*) rather than multipart so
    # auth.js can hash the body byte-for-byte; the HMAC signer in
    # auth.js rejects FormData explicitly because the multipart
    # boundary string is generated only after fetch starts and the
    # pre-fetch hash would therefore mismatch.
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > _AVATAR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="image too large (max 10 MB)")

    # Reuse the canonical cover-ingest pipeline so the avatar gets
    # the same BLAKE2 dedup + WebP encoding + max-1024 resize all
    # album / artist covers go through.
    with get_db_context() as db:
        cover_id = ingest_cover(
            db=db,
            data=data,
            # `external` is the catch-all for non-embedded sources in
            # the covers table CHECK constraint (matches album-cover
            # files on disk and Last.fm artist photos).
            source_type="external",
            source_path="user_profile:avatar",
        )
        if cover_id is None:
            db.rollback()
            raise HTTPException(status_code=500, detail="failed to encode image")
        db.execute(
            text("UPDATE user_profile SET avatar_cover_id = :id WHERE id = 1"),
            {"id": str(cover_id)},
        )
        db.commit()
    return {"avatar_cover_id": str(cover_id)}


@router.delete("/avatar")
def delete_avatar() -> Dict[str, Any]:
    db_execute("UPDATE user_profile SET avatar_cover_id = NULL WHERE id = 1")
    return {"ok": True}


# ============================================================
# Scrobbling toggle (preference, persisted in user_settings)
# ============================================================

# Persisted independently of `lastfm_session_key` so the user can keep
# their Last.fm OAuth connected for enrichment / bio fetches but pause
# scrobbling without re-authorising later. Defaults to True so existing
# connected users keep their previous behaviour.
_SCROBBLING_KEY = "lastfm.scrobbling_enabled"


def _read_scrobbling() -> bool:
    row = db_query_one(
        "SELECT value FROM user_settings WHERE key = %(k)s",
        {"k": _SCROBBLING_KEY},
    )
    if row is None or row.get("value") is None:
        return True
    return bool(row["value"])


class ScrobblingUpdate(BaseModel):
    enabled: bool


@router.get("/scrobbling")
def get_scrobbling() -> Dict[str, Any]:
    return {"enabled": _read_scrobbling()}


@router.put("/scrobbling")
def set_scrobbling(req: ScrobblingUpdate) -> Dict[str, Any]:
    import json
    db_execute(
        """
        INSERT INTO user_settings (key, value) VALUES (%s, %s::jsonb)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                        updated_at = CURRENT_TIMESTAMP
        """,
        (_SCROBBLING_KEY, json.dumps(req.enabled)),
    )
    return {"enabled": req.enabled}


# ============================================================
# Audio chain
# ============================================================

@router.get("/gear")
def list_gear() -> List[Dict[str, Any]]:
    return _load_gear_rows()


@router.get("/gear/system")
def gear_system_analysis() -> Dict[str, Any]:
    """Deterministic pair matrix over the user's park (Gear Advisor
    Phase 3). Pure spec math — cheap enough to compute on read."""
    from gear_pairs import system_analysis
    return system_analysis()


@router.get("/gear/advisor")
def gear_advisor(target: str = "harman", speaker_type: Optional[str] = None,
                 speaker_shape: Optional[str] = None) -> Dict[str, Any]:
    """Upgrade advisor: plateau diagnosis + library axes + candidate
    cards on the price axis (Gear Advisor Phases 4/5). `target` picks
    the measured-matches reference: harman | neutral (no bass shelf);
    speaker_type/speaker_shape filter the spinorama section."""
    from gear_advisor import advisor
    return advisor(target_variant="neutral" if target == "neutral" else "harman",
                   speaker_type=speaker_type, speaker_shape=speaker_shape)


@router.post("/gear/registry/{entry_id}/want")
def promote_registry_entry(entry_id: str) -> Dict[str, Any]:
    """Promote a measurement-registry match into the catalog as a
    'want' candidate: splits brand off the registry's combined name
    (longest known-brand prefix wins, first word otherwise), then runs
    the normal add flow — research + pair-synergy jobs included."""
    entry = db_query_one(
        """
        SELECT id::text AS id, model_name, category::text AS category
        FROM gear_registry_entries WHERE id = %(id)s::uuid
        """,
        {"id": entry_id},
    )
    if not entry:
        raise HTTPException(status_code=404, detail="registry entry not found")

    name = entry["model_name"]
    brands = db_query("SELECT name FROM gear_brands ORDER BY LENGTH(name) DESC")
    brand = next((b["name"] for b in brands
                  if name.lower().startswith(b["name"].lower() + " ")), None)
    if brand:
        model = name[len(brand):].strip()
    else:
        brand, _, model = name.partition(" ")
        model = model.strip() or name
    result = add_gear(GearAddRequest(brand=brand, model=model,
                                     category=entry["category"], status="want"))
    db_execute(
        "UPDATE gear_registry_entries SET gear_model_id = %(g)s::uuid WHERE id = %(id)s::uuid",
        {"g": result["gear_model_id"], "id": entry_id},
    )
    return result


@router.post("/gear")
def add_gear(req: GearAddRequest) -> Dict[str, Any]:
    if req.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid status: {req.status}")
    if req.category not in VALID_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"invalid category: {req.category}")

    # Resolve to a canonical gear_model id, creating brand + model
    # rows if needed (silent input flow).
    if req.model_id:
        gm_id = req.model_id
        existing = db_query_one(
            "SELECT id::text AS id FROM gear_models WHERE id = %(id)s::uuid",
            {"id": gm_id},
        )
        if not existing:
            raise HTTPException(status_code=404, detail="gear model not found")
    else:
        if not (req.brand and req.model):
            raise HTTPException(
                status_code=400,
                detail="brand and model are required when model_id is absent",
            )
        brand_id = _resolve_brand_id(req.brand)
        model = req.model.strip()
        gm_id = str(gear_model_uuid(req.brand.strip(), model, req.category))
        db_execute(
            """
            INSERT INTO gear_models (id, brand_id, model, category, research_state)
            VALUES (%(id)s::uuid, %(brand_id)s::uuid, %(model)s, %(category)s, 'queued')
            ON CONFLICT (id) DO NOTHING
            """,
            {"id": gm_id, "brand_id": brand_id, "model": model, "category": req.category},
        )
        db_execute("SELECT pg_notify('gear_research', %(id)s)", {"id": gm_id})
        from gear_research_worker import request_drain
        request_drain()

    notes = _none_if_blank(req.notes)
    inserted = db_execute(
        """
        INSERT INTO user_gear (gear_model_id, status, notes)
        VALUES (%(gm_id)s::uuid, %(status)s::user_gear_status, %(notes)s)
        ON CONFLICT (gear_model_id) DO UPDATE SET
            removed_at = NULL,
            status = CASE WHEN user_gear.removed_at IS NOT NULL
                          THEN EXCLUDED.status ELSE user_gear.status END,
            notes = COALESCE(EXCLUDED.notes, user_gear.notes)
        RETURNING id::text AS id
        """,
        {"gm_id": gm_id, "status": req.status, "notes": notes},
    )

    # A want-candidate transducer immediately queues pair-synergy
    # research against the owned sources it would hang off — by the
    # time the user opens the advisor, the community voice on those
    # exact pairings is already cached.
    if req.status == "want" and req.category in ("headphones", "iems"):
        from gear_research_worker import enqueue_pair
        own_sources = db_query(
            """
            SELECT ug.gear_model_id::text AS id
            FROM user_gear ug JOIN gear_models gm ON gm.id = ug.gear_model_id
            WHERE ug.status = 'own' AND gm.category IN ('amp', 'player')
              AND ug.removed_at IS NULL
            """
        )
        for src in own_sources:
            enqueue_pair(gm_id, src["id"])
    return {"id": inserted["id"], "gear_model_id": gm_id}


@router.put("/gear/{gear_id}")
def update_gear(gear_id: str, req: GearUpdate) -> Dict[str, Any]:
    if req.status is not None and req.status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"invalid status: {req.status}")

    fields: List[str] = []
    params: Dict[str, Any] = {"id": gear_id}
    if req.status is not None:
        fields.append("status = %(status)s::user_gear_status")
        params["status"] = req.status
    if req.notes is not None:
        fields.append("notes = %(notes)s")
        params["notes"] = _none_if_blank(req.notes)

    if not fields:
        return {"ok": True}

    row = db_execute(
        f"UPDATE user_gear SET {', '.join(fields)} "
        "WHERE id = %(id)s::uuid RETURNING id::text AS id",
        params,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="gear not found")
    return {"ok": True}


class GearReassignRequest(BaseModel):
    brand: str = Field(..., max_length=200)
    model: str = Field(..., max_length=300)


@router.put("/gear/{gear_id}/model")
def reassign_gear_model(gear_id: str, req: GearReassignRequest) -> Dict[str, Any]:
    """Move a user_gear row to a different canonical (brand, model).

    gear_models.id is UUID v5 from (brand_name, model, category), so
    renaming brand and/or model means routing the user_gear row to a
    different canonical row (which may need to be created). The
    category is preserved — categories are a strict enum we don't
    expose here.

    After re-pointing we drop the source gear_models row and source
    gear_brands row if they're now orphaned (no other user_gear or
    gear_models referencing them). This keeps the catalog tight on a
    single-user appliance; P2P peers will re-resolve naturally on
    next sync."""
    brand_name = (req.brand or "").strip()
    model_name = (req.model or "").strip()
    if not brand_name or not model_name:
        raise HTTPException(status_code=400, detail="brand and model are required")

    current = db_query_one(
        """
        SELECT ug.gear_model_id::text AS old_gm_id,
               gm.category,
               gm.brand_id::text AS old_brand_id
        FROM user_gear ug
        JOIN gear_models gm ON gm.id = ug.gear_model_id
        WHERE ug.id = %(id)s::uuid
        """,
        {"id": gear_id},
    )
    if not current:
        raise HTTPException(status_code=404, detail="gear not found")

    new_brand_id = _resolve_brand_id(brand_name)
    new_gm_id = str(gear_model_uuid(brand_name, model_name, current["category"]))

    if new_gm_id == current["old_gm_id"]:
        return {"ok": True, "gear_model_id": new_gm_id, "no_op": True}

    db_execute(
        """
        INSERT INTO gear_models (id, brand_id, model, category, research_state)
        VALUES (%(id)s::uuid, %(brand_id)s::uuid, %(model)s, %(category)s, 'queued')
        ON CONFLICT (id) DO NOTHING
        """,
        {
            "id": new_gm_id,
            "brand_id": new_brand_id,
            "model": model_name,
            "category": current["category"],
        },
    )
    db_execute("SELECT pg_notify('gear_research', %(id)s)", {"id": new_gm_id})

    # Re-point. If the user happens to already own the target model
    # under a different user_gear row, UNIQUE(gear_model_id) on
    # user_gear blocks the update; surface that as a 409 rather than
    # a silent failure.
    try:
        db_execute(
            "UPDATE user_gear SET gear_model_id = %(new)s::uuid WHERE id = %(ug)s::uuid",
            {"new": new_gm_id, "ug": gear_id},
        )
    except Exception as e:
        raise HTTPException(
            status_code=409,
            detail=f"You already have this brand+model in your chain. ({e})",
        )

    db_execute(
        """
        DELETE FROM gear_models
        WHERE id = %(old)s::uuid
          AND NOT EXISTS (SELECT 1 FROM user_gear WHERE gear_model_id = %(old)s::uuid)
        """,
        {"old": current["old_gm_id"]},
    )
    db_execute(
        """
        DELETE FROM gear_brands
        WHERE id = %(old_brand)s::uuid
          AND NOT EXISTS (SELECT 1 FROM gear_models WHERE brand_id = %(old_brand)s::uuid)
        """,
        {"old_brand": current["old_brand_id"]},
    )

    return {"ok": True, "gear_model_id": new_gm_id}


@router.delete("/gear/{gear_id}")
def delete_gear(gear_id: str) -> Dict[str, Any]:
    # Soft-delete: hide from the chain but keep the row (notes/status) so
    # re-adding restores it, and so P2P sync sees a tombstone rather than a
    # vanished row that would resurrect. The updated_at trigger bumps for LWW.
    row = db_execute(
        """UPDATE user_gear SET removed_at = now()
           WHERE id = %(id)s::uuid AND removed_at IS NULL
           RETURNING id::text AS id""",
        {"id": gear_id},
    )
    if row is None:
        raise HTTPException(status_code=404, detail="gear not found")
    return {"ok": True}


# ============================================================
# Peer profile (read-only)
# ============================================================

@router.get("/by-pubkey/{pubkey_prefix}")
def get_peer_profile(pubkey_prefix: str) -> Dict[str, Any]:
    if not pubkey_prefix or len(pubkey_prefix) < 8:
        raise HTTPException(status_code=400, detail="pubkey_prefix too short")

    friend = db_query_one(
        """
        SELECT id, public_key_hex, username, display_name, city, bio,
               avatar_cover_id::text AS avatar_cover_id,
               invite_code, added_at, last_seen
        FROM friends
        WHERE public_key_hex LIKE %(prefix)s || '%%'
          AND is_blocked = FALSE
        ORDER BY public_key_hex
        LIMIT 1
        """,
        {"prefix": pubkey_prefix.lower()},
    )
    if not friend:
        raise HTTPException(status_code=404, detail="peer not found")

    return {
        "public_key_hex":   friend["public_key_hex"],
        "username":         friend["username"],
        "display_name":     friend["display_name"],
        "city":             friend["city"],
        "bio":              friend["bio"],
        "avatar_cover_id":  friend["avatar_cover_id"],
        "invite_code":      friend["invite_code"],
        "added_at":         friend["added_at"],
        "last_seen":        friend["last_seen"],
        # Phase 2: peer audio chain via sync + match indicator computed here.
        "gear":  [],
        "match": None,
    }
