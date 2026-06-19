"""AI canonicalization tier (Layer 2 — judgment over context).

The deterministic layers resolve an artist only by mechanical evidence (content
overlap, exact/unaccent name). The residue needs JUDGMENT over context a rule
can't read: the file PATH embeds the owner's own (often cross-script) canonical
name; the album/genre disambiguate a namesake; world knowledge names a split-
orphan ('Jon' + album 'The Friends of Mr. Cairo' = Jon Anderson, not 1 of 20 MB
'Jon's).

The LLM acts as a CONSTRAINED DISAMBIGUATOR — it returns the gid of a real MB
candidate, or PROPOSE a real artist NAME the backend re-resolves, or SKIP. It can
never invent an MBID. Two guards make it model-tier robust (measured 2026-06-19:
Sonnet ≈ Opus; Haiku makes confident path-confusion errors — both fully caught):
  1. the choice must resolve to a real MB gid (no echo/hallucination),
  2. _name_corresponds: the chosen entity name must match the LIBRARY ARTIST name
     by spelling / transliteration / token-overlap — NOT merely a path token
     (rejects 'London … Orchestra' → 'Kitaro', 'Fenati At Piano' → 'Munich Machine').

Writes confidence='ai' (lowest tier, re-verifiable) and reuses the content-canon
rename/merge machinery, so a resolved artist gets the canonical UUID like any
other tier. Model is chosen by canon policy (best canon-approved the active
provider offers), decoupled from the chat model picker.
"""
import json
import logging
import re
from difflib import SequenceMatcher

from unidecode import unidecode

from database import SessionLocal
from db_pool import db_query
from sqlalchemy import text as _sql
from uuid_utils import artist_uuid
from canon.identity import recanonicalize_artist
from providers import get_provider

logger = logging.getLogger(__name__)

# Canon model policy: pick the best canon-APPROVED model the active provider
# offers (decoupled from the chat picker). Measured — opus/sonnet reliable, haiku
# makes confident path-confusion errors (guarded, so allowed but lowest rank).
_CANON_RANK = {"opus": 3, "sonnet": 2, "haiku": 1}
_STOP = {"the", "a", "an", "and", "of", "his", "her", "with", "de", "la",
         "los", "las", "le", "les", "und", "feat", "ft"}
_BATCH = 12   # artists per AI call


# ─── name-correspondence guard (the model-tier-robustness layer) ──────────

def _latin(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", unidecode(s or "").lower())


def _toks(s: str) -> set:
    return {t for t in _latin(s).split() if t and t not in _STOP}


def _name_corresponds(library_name: str, mb_name: str) -> bool:
    """True iff `mb_name` plausibly names the SAME artist as `library_name` — by
    despaced spelling (punct/diacritic/exact transliteration), significant token
    overlap, or a strong fuzzy ratio (transliteration spelling drift). It does NOT
    accept a mere shared PATH token, because it anchors on the artist name: so
    'London National Philharmonic Orchestra' ↛ 'Kitaro' and 'Fenati At Piano' ↛
    'Munich Machine' are rejected, while 'Jon'→'Jon Anderson', 'Tik'→'ТІК',
    'Valeriy Obodzinskiy'→'Валерий Ободзинский', 'Syn Sun'→'SynSUN' pass."""
    ll, ml = _latin(library_name), _latin(mb_name)
    if not ll.strip() or not ml.strip():
        return False
    if ll.replace(" ", "") == ml.replace(" ", ""):
        return True
    lt, mt = _toks(library_name), _toks(mb_name)
    if lt and mt:
        inter = lt & mt
        shorter = lt if len(lt) <= len(mt) else mt
        if inter and len(inter) / len(shorter) >= 0.5:
            return True
    return SequenceMatcher(None, ll, ml).ratio() >= 0.8


# ─── residue + context ────────────────────────────────────────────────────

def _residue(limit):
    lim = "LIMIT %(l)s" if limit else ""
    return db_query(f"""
        SELECT a.id::text AS id, a.name,
               (SELECT count(*) FROM track_artists ta WHERE ta.artist_id=a.id AND ta.role='primary') AS trk
        FROM artists a
        WHERE EXISTS (SELECT 1 FROM track_artists ta JOIN media_files mf ON mf.track_id=ta.track_id
                      WHERE ta.artist_id=a.id AND ta.role='primary')
          AND NOT EXISTS (SELECT 1 FROM artist_mbids am WHERE am.artist_id=a.id)
        ORDER BY trk DESC, a.name
        {lim}
    """, {"l": limit} if limit else {})


def _context(artist_id, name):
    """Owned albums / genres / one directory path + the f_unaccent MB namesakes."""
    ctx = db_query("""
        SELECT
          (SELECT string_agg(DISTINCT al.title, ' / ') FROM albums al
             JOIN album_artists aa ON aa.album_id=al.id AND aa.artist_id=%(a)s::uuid) AS albums,
          (SELECT string_agg(DISTINCT g.name, ', ') FROM albums al
             JOIN album_artists aa ON aa.album_id=al.id AND aa.artist_id=%(a)s::uuid
             JOIN album_genres ag ON ag.album_id=al.id JOIN genres g ON g.id=ag.genre_id) AS genres,
          (SELECT min(av.directory_path) FROM albums al
             JOIN album_artists aa ON aa.album_id=al.id AND aa.artist_id=%(a)s::uuid
             JOIN album_variants av ON av.album_id=al.id) AS path
    """, {"a": artist_id})[0]
    cands = db_query("""
        SELECT m.gid::text AS gid, m.name, COALESCE(m.comment, '') AS comment
        FROM mb_artist m WHERE f_unaccent(m.name) = f_unaccent(%(n)s)
        ORDER BY m.name LIMIT 25
    """, {"n": name})
    return ctx, cands


# ─── prompt + provider ────────────────────────────────────────────────────

_SYS = (
    "You are an artist-canonicalization disambiguator for a personal FLAC music "
    "library. For each library artist you receive its owned album titles, genres, "
    "and file-system PATH (the path usually embeds the owner's own correct name, "
    "often in Cyrillic). Decide which real MusicBrainz artist it is. Rules: pick a "
    "candidate by its GID, or PROPOSE the real artist's exact name, or SKIP. NEVER "
    "invent a GID. Strong ABSTENTION BIAS: SKIP unless a concrete signal (a path "
    "segment, album, genre, or candidate comment) clearly supports one answer — a "
    "wrong confident pick is far worse than a SKIP. Do not use tools; reason only "
    "from the given data. Reply with ONLY a JSON array, one object per artist: "
    '{"n": <number>, "pick": "<GID | PROPOSE: name | SKIP>", "conf": "high|med|low", '
    '"why": "<=8 words"}.'
)


def _prompt(batch):
    lines = []
    for it in batch:
        ctx, cands = it["ctx"], it["cands"]
        c = " | ".join(f'GID={x["gid"]} {x["name"]}'
                       + (f' ({x["comment"]})' if x["comment"] else "") for x in cands) or "(none)"
        lines.append(
            f'#{it["n"]} "{it["name"]}" | albums: {ctx["albums"] or "-"} | '
            f'genres: {ctx["genres"] or "-"} | path: {ctx["path"] or "-"} | candidates: {c}')
    return "Library artists:\n" + "\n".join(lines)


def _pick_model(provider):
    best, rank = None, -1
    for m in provider.models():
        r = max((v for k, v in _CANON_RANK.items() if k in m.lower()), default=0)
        if r > rank:
            best, rank = m, r
    return best


def _provider():
    r = db_query("SELECT value FROM user_settings WHERE key = 'ai.provider'")
    name = r[0]["value"] if r and r[0]["value"] else None
    for cand in (name, "claude_code", "anthropic"):
        if cand and get_provider(cand):
            return get_provider(cand)
    return None


def _ask(prompt, model, provider):
    res = provider.chat(message=prompt, system_prompt=_SYS, model=model)
    txt = (res.answer or "").strip()
    m = re.search(r"\[.*\]", txt, re.DOTALL)   # strip any prose/markdown fences
    return json.loads(m.group(0)) if m else []


# ─── decision → gid (constrained), then the two guards ────────────────────

def _resolve_choice(pick, cands):
    """A pick string → (gid, mb_name) of a REAL MB entity, or (None, None).
    A GID must be one of the offered candidates; a PROPOSE name must resolve to a
    single MB entity (exact or unaccent). Anything else (incl. SKIP) → none."""
    pick = (pick or "").strip()
    if pick.upper() == "SKIP" or not pick:
        return None, None
    if pick.upper().startswith("PROPOSE:"):
        nm = pick.split(":", 1)[1].strip()
        rows = db_query(
            "SELECT gid::text AS gid, name FROM mb_artist WHERE lower(name)=lower(%(n)s) "
            "UNION SELECT gid::text, name FROM mb_artist WHERE f_unaccent(name)=f_unaccent(%(n)s)",
            {"n": nm})
        return (rows[0]["gid"], rows[0]["name"]) if len(rows) == 1 else (None, None)
    by_gid = {c["gid"]: c["name"] for c in cands}
    return (pick, by_gid[pick]) if pick in by_gid else (None, None)


def _apply(artist_id, mb_name, gid):
    db = SessionLocal()
    try:
        canon_id = artist_id
        if str(artist_uuid(mb_name)) != str(artist_id):
            canon_id = recanonicalize_artist(db, artist_id, mb_name)
        db.execute(_sql(
            "INSERT INTO artist_mbids (mbid, artist_id, confidence) VALUES (:m, :a, 'ai') "
            "ON CONFLICT (mbid) DO UPDATE SET artist_id = EXCLUDED.artist_id"),
            {"m": gid, "a": canon_id})
        db.commit()
    finally:
        db.close()


# ─── entry point ──────────────────────────────────────────────────────────

def ai_canonize(limit: int = None, dry_run: bool = False) -> dict:
    provider = _provider()
    if not provider:
        return {"skipped": "no AI provider configured", "canonized": 0}
    model = _pick_model(provider)
    st = {"provider": provider.name, "model": model, "considered": 0,
          "canonized": 0, "skipped": 0, "guard_rejected": 0, "errors": 0, "examples": []}

    artists = _residue(limit)
    enriched = [{"n": i + 1, "id": a["id"], "name": a["name"],
                 **dict(zip(("ctx", "cands"), _context(a["id"], a["name"])))}
                for i, a in enumerate(artists)]
    st["considered"] = len(enriched)

    for s in range(0, len(enriched), _BATCH):
        batch = enriched[s:s + _BATCH]
        by_n = {it["n"]: it for it in batch}
        try:
            decisions = _ask(_prompt(batch), model, provider)
        except Exception as e:
            st["errors"] += 1
            logger.error("ai_canon batch %d failed: %s", s // _BATCH, e)
            continue
        for d in decisions:
            it = by_n.get(d.get("n"))
            if not it:
                continue
            gid, mb_name = _resolve_choice(d.get("pick"), it["cands"])
            if not gid:
                st["skipped"] += 1
                continue
            if not _name_corresponds(it["name"], mb_name):   # the path-confusion guard
                st["guard_rejected"] += 1
                logger.info("ai_canon GUARD rejected %r -> %r (%s)", it["name"], mb_name, d.get("why"))
                continue
            if dry_run:
                st["canonized"] += 1
                if len(st["examples"]) < 25:
                    st["examples"].append({"from": it["name"], "to": mb_name,
                                           "conf": d.get("conf"), "why": d.get("why")})
                continue
            try:
                _apply(it["id"], mb_name, gid)
                st["canonized"] += 1
            except Exception as e:
                st["errors"] += 1
                logger.error("ai_canon apply failed for %r: %s", it["name"], e)
    logger.info("ai_canon: %s", {k: v for k, v in st.items() if k != "examples"})
    return st
