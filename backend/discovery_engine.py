"""Discovery search engine — tool-driven composable query builder.

Replaces routers/discovery.py, the filter logic in search.py (_filter_clauses /
_apply_filters), and the MCP search tools with ONE builder. See
docs/design/DISCOVERY-SEARCH-ENGINE.md (§Tools/§Bridges).

Model:
  Tool   = one UI control (text search, Vocalist, Gender, Energy, Instruments…).
           Declares its sources; `broad` marks non-selective gates whose browse
           roll-ups would rank by catalog size instead of signal.
  Source = one signal with a score contract: is_gate sources are binary filters
           (WHERE, no rank, ALL targets); the rest are relevance sources scored to
           [0,1], normalized per-source (calibrated min-max), and scoped by
           `targets` — lexical identity scores only its own entity level, the
           characteristic signals (clap/lyrics) also aggregate up via AVG.
  Bridge = static, corpus-aware edge registry; BFS composes the shortest table
           path from a source's table to the target (dedup shared joins). corpus
           SELECTS a bridge (owned→media_files, phantom→album_tracks, all→both),
           not just a WHERE filter.

STATUS: Phase 1 SKELETON for review. Real: registries (entities, tools/sources,
bridge edges) and the normalization expression. Stubbed with TODO: BFS routing,
per-target assembly, relevance-vs-gate weaving, aggregable HAVING, cover/media
surfacing. floor/ceil are placeholders — calibration is Phase 2.
"""
from dataclasses import dataclass
from typing import Any, Callable, Optional


# ── Entities ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EntityDef:
    key: str
    pk: str
    name_col: str
    latin_col: str
    table: str          # own table + alias, e.g. "artists a"
    default_order: str  # browse-mode ordering (no relevance signal active)
    surface: str = ""   # extra SELECT columns for tiles (is_owned, cover_id, media_file_id, …)


ENTITIES: dict[str, EntityDef] = {
    "artist": EntityDef("artist", "a.id", "a.name", "a.name_latin", "artists a",
                        "(SELECT COUNT(*) FROM track_artists ta "
                        "WHERE ta.artist_id=a.id AND ta.role='primary') DESC",
                        surface=", a.gender, a.is_vocalist, "
                        "EXISTS (SELECT 1 FROM track_artists ta JOIN media_files mf "
                        "ON mf.track_id=ta.track_id WHERE ta.artist_id=a.id) AS is_owned, "
                        "(SELECT mf.cover_id::text FROM track_artists ta JOIN media_files mf "
                        "ON mf.track_id=ta.track_id WHERE ta.artist_id=a.id AND mf.cover_id IS NOT NULL "
                        "LIMIT 1) AS cover_id, "
                        "(SELECT mf.id FROM track_artists ta JOIN media_files mf "
                        "ON mf.track_id=ta.track_id AND ta.role='primary' WHERE ta.artist_id=a.id "
                        "LIMIT 1) AS media_file_id"),
    "album":  EntityDef("album", "al.id", "al.title", "al.title_latin", "albums al",
                        "al.release_year DESC NULLS LAST, al.title",
                        surface=", al.release_year AS year, al.cover_url, "
                        "(SELECT a.name FROM album_artists aa JOIN artists a ON a.id=aa.artist_id "
                        "WHERE aa.album_id=al.id AND aa.role='primary' LIMIT 1) AS artist, "
                        "EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id=al.id) AS is_owned, "
                        "(SELECT mf.cover_id::text FROM album_variants av JOIN media_files mf "
                        "ON mf.album_variant_id=av.id WHERE av.album_id=al.id AND mf.cover_id IS NOT NULL "
                        "LIMIT 1) AS cover_id, "
                        "(SELECT mf.id FROM album_variants av JOIN media_files mf ON mf.album_variant_id=av.id "
                        "WHERE av.album_id=al.id ORDER BY mf.disc_number NULLS FIRST, mf.track_number "
                        "LIMIT 1) AS media_file_id"),
    "track":  EntityDef("track", "t.id", "t.title", "t.title_latin", "tracks t",
                        "t.title",
                        surface=", (SELECT a.name FROM track_artists ta JOIN artists a ON a.id=ta.artist_id "
                        "WHERE ta.track_id=t.id AND ta.role='primary' LIMIT 1) AS artist, "
                        "EXISTS (SELECT 1 FROM media_files mf WHERE mf.track_id=t.id) AS is_owned, "
                        "(SELECT mf.id FROM media_files mf WHERE mf.track_id=t.id "
                        "ORDER BY mf.is_analysis_source DESC LIMIT 1) AS media_file_id, "
                        "(SELECT mf.cover_id::text FROM media_files mf WHERE mf.track_id=t.id "
                        "AND mf.cover_id IS NOT NULL LIMIT 1) AS cover_id, "
                        "(SELECT mf.duration_seconds FROM media_files mf WHERE mf.track_id=t.id LIMIT 1) "
                        "AS duration_seconds, "
                        "(SELECT al.title FROM media_files mf JOIN album_variants av ON av.id=mf.album_variant_id "
                        "JOIN albums al ON al.id=av.album_id WHERE mf.track_id=t.id LIMIT 1) AS album, "
                        "(SELECT al.release_year FROM media_files mf JOIN album_variants av ON av.id=mf.album_variant_id "
                        "JOIN albums al ON al.id=av.album_id WHERE mf.track_id=t.id LIMIT 1) AS year"),
}


# ── Sources & tools ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Source:
    key: str
    table: str                 # table the score_sql reads (bridge routing anchor)
    score_sql: str             # raw score in [0,1] (relevance) OR a boolean predicate (gate)
    targets: tuple = ()        # entity levels this source's SCORE reaches (gates ignore this —
                               # they always apply to every target via the atom conjunction).
                               # Lexical identity sources score ONLY their own level: search
                               # results are entity-scoped matches, the neighbourhood comes via
                               # navigation (Valerii) — name-down flooded tracks/albums that
                               # match the query in their OWN title. Characteristic sources
                               # (clap/lyrics) score their level + aggregate UP via AVG.
    is_gate: bool = False      # binary filter: pure WHERE, contributes no rank
    floor: float = 0.0         # relevance threshold — also the WHERE cutoff (calibrate: Phase 2)
    ceil: float = 1.0          # "strong match" — norm caps here (calibrate: Phase 2)
    weight: float = 1.0        # importance in the cross-tool sum
    model: Optional[str] = None   # model_cache key this source needs; gracefully skipped if cold
    level: str = ""            # entity level (track/album/artist); default derived from .table
    retrieve: str = ""         # index-friendly retrieve-branch predicate; default (score_sql) >= floor
    needs_join: frozenset = frozenset()
    expand: Optional[Callable[[Any], Any]] = None


@dataclass(frozen=True)
class Tool:
    key: str
    sources: tuple             # tuple[Source, ...]
    broad: bool = False        # non-selective gate (mode ~51%): browse roll-ups above the
                               # atom are skipped when every active tool is broad — ranking
                               # by count then degenerates to catalog size (corr 0.94)


TOOLS: dict[str, Tool] = {
    # Multi-source relevance. Each source scored + normalized; the tool's
    # contribution is GREATEST(norm over its sources) — name OR bio OR ….
    "text": Tool("text", sources=(
        # Trigram retrieve predicates use the GIN-indexed `%` operator (its
        # pg_trgm.similarity_threshold default 0.3 == these sources' floor) +
        # prefix LIKE — `similarity(col,q) >= x` can't use the index and
        # seq-scans 3M-row joins under corpus=all (proven: shm blowup).
        Source("artist_name", "artists",
               "GREATEST(similarity(a.name_latin,:ql), "
               "CASE WHEN a.name_latin LIKE :qlpfx THEN 0.85 ELSE 0 END, "
               "similarity(a.name,:q))",     # original-script exact match too
               targets=("artist",), floor=0.3, ceil=1.0, weight=1.0,
               retrieve="(a.name_latin % :ql OR a.name_latin LIKE :qlpfx OR a.name % :q)"),
        Source("artist_alias", "artist_name_aliases",   # 0b: CJK cutlet + human file-tag readings
               "GREATEST(similarity(ana.alias_latin,:ql), "
               "CASE WHEN ana.alias_latin LIKE :qlpfx THEN 0.85 ELSE 0 END)",
               targets=("artist",), floor=0.3, ceil=1.0, weight=1.0,
               retrieve="(ana.alias_latin % :ql OR ana.alias_latin LIKE :qlpfx)",
               needs_join=frozenset({"artist_name_aliases"})),
        Source("artist_bio", "artist_bio_embeddings", "1-(abe.vector <=> CAST(:qvec AS vector))",
               targets=("artist",), floor=0.5, ceil=0.85, weight=0.7, model="enrichment",
               needs_join=frozenset({"artist_bio_embeddings"})),
        Source("album_title", "albums",
               "GREATEST(similarity(al.title_latin,:ql), "
               "CASE WHEN al.title_latin LIKE :qlpfx THEN 0.85 ELSE 0 END)",
               targets=("album",), floor=0.3, ceil=1.0, weight=1.0,
               retrieve="(al.title_latin % :ql OR al.title_latin LIKE :qlpfx)"),
        Source("track_title", "tracks",
               "GREATEST(similarity(t.title_latin,:ql), "
               "CASE WHEN t.title_latin LIKE :qlpfx THEN 0.85 ELSE 0 END)",
               targets=("track",), floor=0.3, ceil=1.0, weight=1.0,
               retrieve="(t.title_latin % :ql OR t.title_latin LIKE :qlpfx)"),
        Source("clap", "embeddings", "1-(e.vector <=> CAST(:qclap AS vector))",
               targets=("track", "album", "artist"),   # characteristic: aggregates up
               floor=0.25, ceil=0.45, weight=0.8, model="clap",  # CLAP text→audio: low scale
               needs_join=frozenset({"embeddings"})),
        Source("lyrics_sem", "lyrics_embeddings", "1-(le.vector <=> CAST(:qlyr AS vector))",
               targets=("track", "album", "artist"),
               floor=0.5, ceil=0.75, weight=0.6, model="lyrics",
               needs_join=frozenset({"lyrics_embeddings"})),
    )),
    # Binary gates (artist-level).
    "vocalist": Tool("vocalist", sources=(
        Source("vocalist", "artists", "a.is_vocalist = :vocalist", is_gate=True),)),
    "gender": Tool("gender", sources=(
        Source("gender", "artists", "a.gender = :gender", is_gate=True),)),
    # Track-level gates. mode/danceable/energy are BROAD (mode splits the library
    # ~50/50) — see Tool.broad.
    "energy": Tool("energy", broad=True, sources=(
        Source("energy", "audio_features", "af.energy_db BETWEEN :energy_lo AND :energy_hi",
               is_gate=True, needs_join=frozenset({"audio_features"})),)),
    "instruments": Tool("instruments", sources=(
        Source("instruments", "audio_features", "af.instruments ?| :instruments",
               is_gate=True, needs_join=frozenset({"audio_features"}),
               expand=lambda v: _expand_instruments(v)),)),
    "moods": Tool("moods", sources=(
        Source("moods", "audio_features", "af.moods ?| :moods", is_gate=True,
               needs_join=frozenset({"audio_features"})),)),
    "bpm": Tool("bpm", sources=(
        Source("bpm", "audio_features", "af.bpm BETWEEN :bpm_min AND :bpm_max",
               is_gate=True, needs_join=frozenset({"audio_features"})),)),
    "key": Tool("key", sources=(
        Source("key", "audio_features", "af.key = :key", is_gate=True,
               needs_join=frozenset({"audio_features"})),)),
    "mode": Tool("mode", broad=True, sources=(
        Source("mode", "audio_features", "af.mode = :mode", is_gate=True,
               needs_join=frozenset({"audio_features"})),)),
    "danceable": Tool("danceable", broad=True, sources=(
        Source("danceable", "audio_features", "af.danceability >= 0.5", is_gate=True,
               needs_join=frozenset({"audio_features"})),)),
    # Album-level.
    "genre": Tool("genre", sources=(
        Source("genre", "genres", "g.name = ANY(:genre)", is_gate=True,
               needs_join=frozenset({"album_genres", "genres"})),)),
    "year": Tool("year", sources=(
        Source("year", "albums", "al.release_year BETWEEN :year_from AND :year_to",
               is_gate=True),)),
    # Semantic-only track relevance (no title): CLAP text→audio, lyrics text→lyrics.
    "sound": Tool("sound", sources=(
        Source("sound", "embeddings", "1-(e.vector <=> CAST(:qclap AS vector))",
               targets=("track", "album", "artist"),
               floor=0.25, ceil=0.45, weight=1.0, model="clap",
               needs_join=frozenset({"embeddings"})),)),
    "lyrics": Tool("lyrics", sources=(
        Source("lyrics", "lyrics_embeddings", "1-(le.vector <=> CAST(:qlyr AS vector))",
               targets=("track", "album", "artist"),
               floor=0.5, ceil=0.75, weight=1.0, model="lyrics",
               needs_join=frozenset({"lyrics_embeddings"})),)),
}


# ── Bridge registry (static, corpus-aware) ──────────────────────────────────

@dataclass(frozen=True)
class Edge:
    a: str            # table
    b: str            # table
    join_sql: str     # ON fragment used when b is joined onto a path already holding a
    corpus: str = "all"   # "owned" | "phantom" | "all" — which layer this edge serves


# Undirected. Two track↔album edges by design (the doc's proof case): album_tracks
# is phantom-only (1 hop), media_files→album_variants is owned-only (2 hops). A
# corpus-blind BFS would take album_tracks and lose 87% of owned tracks — so the
# corpus filter on edges is load-bearing, not cosmetic.
EDGES: tuple = (
    Edge("artists", "track_artists", "ta.artist_id = a.id"),
    Edge("track_artists", "tracks", "ta.track_id = t.id"),
    Edge("artists", "album_artists", "aa.artist_id = a.id"),
    Edge("album_artists", "albums", "aa.album_id = al.id"),
    Edge("tracks", "audio_features", "af.track_id = t.id"),
    Edge("tracks", "embeddings", "e.track_id = t.id"),
    Edge("tracks", "lyrics_embeddings", "le.track_id = t.id"),
    Edge("artists", "artist_bio_embeddings", "abe.artist_id = a.id"),
    Edge("artists", "artist_name_aliases", "ana.artist_id = a.id"),
    Edge("tracks", "album_tracks", "atr.track_id = t.id", corpus="phantom"),
    Edge("album_tracks", "albums", "atr.album_id = al.id", corpus="phantom"),
    Edge("tracks", "media_files", "mf.track_id = t.id", corpus="owned"),
    Edge("media_files", "album_variants", "mf.album_variant_id = av.id", corpus="owned"),
    Edge("album_variants", "albums", "av.album_id = al.id", corpus="owned"),
    Edge("albums", "album_genres", "ag.album_id = al.id"),
    Edge("album_genres", "genres", "g.id = ag.genre_id"),
)


# table → alias used in EDGES.join_sql fragments and subquery FROM/JOINs
_ALIAS = {
    "artists": "a", "albums": "al", "tracks": "t",
    "track_artists": "ta", "album_artists": "aa", "album_tracks": "atr",
    "audio_features": "af", "embeddings": "e", "artist_bio_embeddings": "abe",
    "media_files": "mf", "album_variants": "av", "album_genres": "ag", "genres": "g",
    "artist_name_aliases": "ana", "lyrics_embeddings": "le",
}


def _route(src_table: str, target: EntityDef, corpus: str) -> list:
    """BFS over corpus-usable EDGES → shortest path from the TARGET table to
    src_table. Returns [(table, join_sql), ...] starting at the edge adjacent to
    the target and ending at src_table; the first element's join_sql correlates to
    the target row. Empty when src IS the target table. corpus='owned'/'phantom'
    drops the other layer's edges (corpus='all' branching is step 3)."""
    from collections import deque

    tgt = target.table.split()[0]
    if src_table == tgt:
        return []
    adj: dict = {}
    for e in EDGES:
        if corpus != "all" and e.corpus not in ("all", corpus):
            continue
        adj.setdefault(e.a, []).append((e.b, e.join_sql))
        adj.setdefault(e.b, []).append((e.a, e.join_sql))

    q = deque([(tgt, [])])
    seen = {tgt}
    while q:
        node, path = q.popleft()
        if node == src_table:
            return path
        for nxt, js in adj.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                q.append((nxt, path + [(nxt, js)]))
    raise ValueError(f"no bridge {tgt} → {src_table} under corpus={corpus}")


def _subquery_from(path: list) -> tuple[str, str]:
    """From a _route() path build (FROM+JOINs, correlation predicate). The first
    hop's join_sql is the correlation to the outer target row; the rest are JOINs."""
    first_tbl, corr = path[0]
    frm = f"{first_tbl} {_ALIAS[first_tbl]}"
    for tbl, js in path[1:]:
        frm += f" JOIN {tbl} {_ALIAS[tbl]} ON {js}"
    return frm, corr


def _exists_from(src: Source, path: list) -> str:
    frm, corr = _subquery_from(path)
    return f"EXISTS (SELECT 1 FROM {frm} WHERE {corr} AND {src.score_sql})"


def _exists_gate(src: Source, entity: EntityDef, corpus: str) -> str:
    """A below-target gate → EXISTS over the bridge path (genre on a track target →
    'track has an album with this genre'). Under corpus='all', a source that crosses
    the owned/phantom boundary (track↔album) has TWO distinct bridges — owned
    (media_files→album_variants) and phantom (album_tracks) — so OR both EXISTS;
    otherwise the shorter phantom path would silently drop 87% of owned tracks."""
    if corpus == "all":
        po = _route(src.table, entity, "owned")
        pp = _route(src.table, entity, "phantom")
        if po != pp:
            return f"({_exists_from(src, po)} OR {_exists_from(src, pp)})"
    return _exists_from(src, _route(src.table, entity, corpus))


def _lateral_relevance(src: Source, entity: EntityDef, corpus: str) -> tuple[str, str, str]:
    """A below/child relevance source → LEFT JOIN LATERAL over the bridge taking
    MAX(norm), so 1:N (bio chunks) and 1:1 (clap/energy) both work. Returns
    (lateral_join_sql, score_ref, floor_predicate)."""
    frm, corr = _subquery_from(_route(src.table, entity, corpus))
    alias = f"{src.key}_j"
    join = (f"LEFT JOIN LATERAL (SELECT MAX({_norm_expr(src)}) AS s "
            f"FROM {frm} WHERE {corr}) {alias} ON true")
    return join, f"COALESCE({alias}.s, 0)", f"{alias}.s > 0"


# Ownership is derived, never flagged: owned ⟺ the entity has a media_files row.
_OWNED_GUARD = {
    "artist": "EXISTS (SELECT 1 FROM track_artists ta JOIN media_files mf "
              "ON mf.track_id=ta.track_id WHERE ta.artist_id=a.id)",
    "album":  "EXISTS (SELECT 1 FROM album_variants av WHERE av.album_id=al.id)",
    "track":  "EXISTS (SELECT 1 FROM media_files mf WHERE mf.track_id=t.id)",
}


def _corpus_clause(entity: EntityDef, corpus: str) -> Optional[str]:
    """owned/phantom → an EXISTS(/NOT EXISTS) guard on the target's own table; 'all'
    → no guard. This is also the perf-critical narrowing: it cuts a track target
    from ~3M to ~37k before any per-row vector LATERAL runs."""
    guard = _OWNED_GUARD.get(entity.key)
    if not guard or corpus == "all":
        return None
    return guard if corpus == "owned" else f"NOT {guard}"


# ── Score normalization ─────────────────────────────────────────────────────

def _norm_expr(src: Source) -> str:
    """Calibrated per-source min-max in SQL: clamp((raw - floor)/(ceil - floor), 0, 1).
    Makes 0.5 mean the same across sources so scores can be summed for ORDER BY."""
    span = src.ceil - src.floor
    return f"LEAST(1.0, GREATEST(0.0, (({src.score_sql}) - {src.floor}) / {span}))"


# ── Source levels + atom resolution ─────────────────────────────────────────

# A source's entity level = the grain its signal lives at. The bridge graph pulls
# it up/down; the level decides the atom and the target range. Derived from .table
# unless a Source overrides it (genre reads `genres` but is an album-grain filter).
_TABLE_LEVEL = {
    "artists": "artist", "artist_name_aliases": "artist", "artist_bio_embeddings": "artist",
    "albums": "album", "album_artists": "album", "album_genres": "album", "genres": "album",
    "tracks": "track", "audio_features": "track", "embeddings": "track",
    "lyrics_embeddings": "track", "album_tracks": "track", "media_files": "track",
    "track_artists": "track",
}
_LEVEL_RANK = {"track": 0, "album": 1, "artist": 2}   # atom = the MIN (finest) rank


def _level(src: Source) -> str:
    return src.level or _TABLE_LEVEL[src.table]


# ── Builder ─────────────────────────────────────────────────────────────────

def build(active: dict, query: dict, corpus: str = "all", limit: int = 20) -> dict:
    """active = {tool_key: value}. ATOM-CENTRIC composition: the atom is the LOWEST
    level among all active sources (track < album < artist). matched(atom) applies
    every tool — atom-level relevance sources OR (union-retrieve), gates AND (all
    levels, promoted via EXISTS). Results are produced for the atom entity and every
    level ABOVE it (never below): the atom target ranks by relevance; each higher
    target combines its OWN-level identity sources with an AVG roll-up of the
    matched atoms' characteristic scores — 'romantic sax + female vocal' rolls up
    to the artists whose catalogue sounds like that (Sade), while a Gender-only
    query (atom=artist) yields only artists. Governor: a browse where every active
    tool is broad (mode ~51% of the library) skips the above-atom roll-ups —
    count-ranking there degenerates to catalog size (corr 0.94). Returns
    {target_key: (sql, params)}."""
    tools = [TOOLS[k] for k in active
             if k in TOOLS and active.get(k) not in (None, "", "any", [])]
    if not tools:
        return {}
    atom_lvl = min((_level(s) for t in tools for s in t.sources),
                   key=lambda lv: _LEVEL_RANK[lv])
    atom = ENTITIES[atom_lvl]
    relevance_active = any(not s.is_gate for t in tools for s in t.sources)
    skip_rollups = not relevance_active and all(t.broad for t in tools)
    out: dict = {}
    for lvl, rank in _LEVEL_RANK.items():
        if rank < _LEVEL_RANK[atom_lvl] or lvl not in ENTITIES:
            continue
        if rank > _LEVEL_RANK[atom_lvl] and skip_rollups:
            continue
        out[lvl] = (_build_atom(atom, tools, active, corpus, limit) if lvl == atom_lvl
                    else _build_higher(atom, ENTITIES[lvl], tools, active, corpus, limit))
    return out


def _retrieve_branch(src: Source, entity: EntityDef, corpus: str, gates: list, K: int) -> str:
    """One relevance source → SELECT the target pk, top-K via the source's own index,
    gates + corpus pushed down so the top-K is already filtered. same-table: WHERE
    floor ORDER BY score. Bridged: join the source in along the route path and order
    by its score (HNSW for vectors, trigram for names)."""
    et = entity.table.split()[0]
    conds = list(gates)
    cg = _corpus_clause(entity, corpus)
    if cg:
        conds.append(cg)
    conds.append(src.retrieve or f"({src.score_sql}) >= {src.floor}")
    if src.table == et:
        frm = entity.table
    else:
        path = _route(src.table, entity, corpus)
        frm = entity.table + " " + " ".join(f"JOIN {t} {_ALIAS[t]} ON {js}" for t, js in path)
    return (f"SELECT {entity.pk} AS id FROM {frm} WHERE {' AND '.join(conds)} "
            f"ORDER BY ({src.score_sql}) DESC LIMIT {K}")


def _matched_core(atom, tools, active, corpus, K=500):
    """The retrieve→rerank pieces at the atom level, shared by the atom target and
    every higher aggregation. Gates come from EVERY active tool (AND, promoted to the
    atom via _exists_gate) — NOT filtered by target, which is what let cross-level
    filters (female+vocal on a track atom) silently drop out before. Relevance is
    ATOM-LEVEL SOURCES ONLY (lexical identity doesn't cross levels — no name-down
    flooding); each tool yields two score expressions: at_terms scores the atom
    target, up_terms only the sources whose targets reach above the atom (the
    characteristic channel that roll-ups AVG). Returns (gates, branches, at_terms,
    up_terms, lateral)."""
    et = atom.table.split()[0]
    gates = [src.score_sql if src.table == et else _exists_gate(src, atom, corpus)
             for tool in tools for src in tool.sources if src.is_gate]
    branches: list[str] = []
    at_terms: list[str] = []
    up_terms: list[str] = []
    lateral: list[str] = []
    for tool in tools:
        at_norms: list[str] = []
        up_norms: list[str] = []
        for src in tool.sources:
            if src.is_gate or _level(src) != atom.key:
                continue
            if src.model and not _model_ready(src.model):
                _kick_model(src.model)      # skip cold-model source, warm it for next request
                continue
            if src.table == et:
                norm = _norm_expr(src)
            else:
                lat, ref, _ = _lateral_relevance(src, atom, corpus)
                lateral.append(lat)
                norm = ref
            # Weight INSIDE the GREATEST: an exact title (w=1.0) must outrank a
            # lyrics mention (w=0.6) — with an outer max-weight they tied at 1.0.
            at_norms.append(f"{src.weight} * {norm}")
            if any(_LEVEL_RANK[t] > _LEVEL_RANK[atom.key] for t in src.targets):
                up_norms.append(f"{src.weight} * {norm}")
            branches.append(_retrieve_branch(src, atom, corpus, gates, K))
        if at_norms:
            at_terms.append(f"GREATEST({', '.join(at_norms)})")
        if up_norms:
            up_terms.append(f"GREATEST({', '.join(up_norms)})")
    return gates, branches, at_terms, up_terms, lateral


def _matched_sql(atom, gates, branches, at_terms, up_terms, lateral, corpus):
    """(matched-body, ctes-prefix). matched exposes (id, s_at, s_up) at the atom
    level. Browse (no relevance branches) → every gate-matching atom, NULL scores
    (roll-ups then rank by count). Relevance without any up-eligible source →
    s_up = 0 so roll-ups pull in nothing (a title-only match must not mint
    artist/album candidates)."""
    cg = _corpus_clause(atom, corpus)
    filt = list(gates) + ([cg] if cg else [])
    where = (" WHERE " + " AND ".join(filt)) if filt else ""
    if not branches:
        return (f"SELECT {atom.pk} AS id, NULL::float AS s_at, NULL::float AS s_up "
                f"FROM {atom.table}{where}"), ""
    s_at = " + ".join(at_terms)
    s_up = " + ".join(up_terms) if up_terms else "0.0"
    cand = " UNION ".join(f"({b})" for b in branches)
    body = (f"SELECT {atom.pk} AS id, ({s_at}) AS s_at, ({s_up}) AS s_up "
            f"FROM (SELECT DISTINCT id FROM cand) c JOIN {atom.table} ON {atom.pk}=c.id "
            f"{' '.join(lateral)}{where}")
    return body, f"cand AS ({cand}), "


def _build_atom(atom, tools, active, corpus, limit):
    """Atom target: rerank the bounded candidate set on the full normalized score
    (GREATEST within a tool, weighted sum across tools), gates + corpus in WHERE."""
    gates, branches, at_terms, _, lateral = _matched_core(atom, tools, active, corpus)
    cg = _corpus_clause(atom, corpus)
    filt = list(gates) + ([cg] if cg else [])
    where = (" WHERE " + " AND ".join(filt)) if filt else ""
    if not branches:                       # gates-only / browse
        sql = (f"SELECT {atom.pk} AS id, {atom.name_col} AS name, NULL AS score{atom.surface} "
               f"FROM {atom.table}{where} ORDER BY {atom.default_order} LIMIT {int(limit)}")
        return sql, _bind_params(tools, active)
    score = " + ".join(at_terms)
    cand = " UNION ".join(f"({b})" for b in branches)
    sql = (f"WITH cand AS ({cand}) "
           f"SELECT {atom.pk} AS id, {atom.name_col} AS name, ({score}) AS score{atom.surface} "
           f"FROM (SELECT DISTINCT id FROM cand) c JOIN {atom.table} ON {atom.pk}=c.id "
           f"{' '.join(lateral)}{where} "
           f"ORDER BY ({score}) DESC, {atom.default_order} LIMIT {int(limit)}")
    return sql, _bind_params(tools, active)


# Weight of the roll-up channel vs own-level relevance in a higher target's score.
# Own-level identity (an artist MATCHING "Madonna" by name) must dominate incidental
# roll-up (an artist who merely HAS a track titled "Madonna"). Calibration knob.
_ROLLUP_W = 0.5


def _build_higher(atom, L, tools, active, corpus, limit, K=500):
    """Higher target = two channels. Roll-up: AVG of the matched atoms' s_up — the
    CHARACTERISTIC channel only (clap/lyrics), never lexical identity: 'the artists
    whose catalogue sounds like this' (Sade for romantic-sax), NOT 'artists who have
    a track titled like this'. Own-level: relevance sources whose targets include L
    (artist name/alias/bio on an artist target) scored on the L row directly —
    identity matches, including track-less phantom stubs the roll-up can't reach.
    Gates of level ≥ L re-apply on the L row itself (Gender:Female means the ARTIST
    list holds only females, not males who share a matched track). ORDER by combined
    score, then matched-atom COUNT (browse mode: most matching atoms)."""
    gates_a, branches_a, at_terms, up_terms, lat_a = _matched_core(atom, tools, active, corpus)
    body, ctes = _matched_sql(atom, gates_a, branches_a, at_terms, up_terms, lat_a, corpus)
    link = _agg_link(atom, L, corpus)
    Lt = L.table.split()[0]
    Lrank = _LEVEL_RANK[L.key]
    gate_srcs = [s for t in tools for s in t.sources if s.is_gate]
    gates_L = [s.score_sql if s.table == Lt else _exists_gate(s, L, corpus)
               for s in gate_srcs if _LEVEL_RANK[_level(s)] >= Lrank]
    cg = _corpus_clause(L, corpus)
    if cg:
        gates_L.append(cg)

    own_terms: list[str] = []
    own_branches: list[str] = []
    own_lat: list[str] = []
    for tool in tools:
        norms: list[str] = []
        for src in tool.sources:
            if src.is_gate or _level(src) != L.key or L.key not in src.targets:
                continue
            if src.model and not _model_ready(src.model):
                _kick_model(src.model)
                continue
            if src.table == Lt:
                norms.append(f"{src.weight} * {_norm_expr(src)}")
            else:
                lat, ref, _ = _lateral_relevance(src, L, corpus)
                own_lat.append(lat)
                norms.append(f"{src.weight} * {ref}")
            own_branches.append(_retrieve_branch(src, L, corpus, gates_L, K))
        if norms:
            own_terms.append(f"GREATEST({', '.join(norms)})")

    # Strict conjunction: with a below-L gate active (sax, minor…) an L row must own
    # a matched atom — an own-level candidate alone can't prove the gates hold on ONE
    # atom together, so the own channel then only adds score, not candidacy.
    if any(_LEVEL_RANK[_level(s)] < Lrank for s in gate_srcs):
        own_branches = []

    cand = "SELECT lid FROM rolled"
    if own_branches:
        cand += " UNION " + " UNION ".join(
            f"SELECT id AS lid FROM ({b}) ob{i}" for i, b in enumerate(own_branches))
    score = (" + ".join(own_terms + [f"{_ROLLUP_W} * COALESCE(r.s, 0)"])
             if (at_terms or own_terms) else "NULL")
    where = (" WHERE " + " AND ".join(gates_L)) if gates_L else ""
    # s_up > 0 keeps lexical-only matches out of the roll-up (they'd mint zero-score
    # candidates); NULL passes for browse mode, where roll-ups rank by count.
    sql = (f"WITH {ctes}matched AS ({body}), "
           f"rolled AS (SELECT lk.lid AS lid, AVG(m.s_up) AS s, COUNT(*) AS n "
           f"FROM matched m {link} WHERE m.s_up > 0 OR m.s_up IS NULL GROUP BY lk.lid), "
           f"slim AS (SELECT {L.pk} AS id, ({score}) AS score, r.n AS n "
           f"FROM (SELECT DISTINCT lid FROM ({cand}) c0) cd "
           f"JOIN {L.table} ON {L.pk} = cd.lid "
           f"LEFT JOIN rolled r ON r.lid = {L.pk} "
           f"{' '.join(own_lat)}{where} "
           f"ORDER BY 2 DESC NULLS LAST, r.n DESC NULLS LAST, {L.default_order} "
           f"LIMIT {int(limit)}) "
           f"SELECT {L.pk} AS id, {L.name_col} AS name, slim.score{L.surface} "
           f"FROM slim JOIN {L.table} ON {L.pk} = slim.id "
           f"ORDER BY slim.score DESC NULLS LAST, slim.n DESC NULLS LAST")
    return sql, _bind_params(tools, active)


def _agg_link(atom, L, corpus):
    """LATERAL yielding L's pk(s) for a matched atom row (m.id), corpus two-path aware:
    under corpus='all' a track rolls up to its owned album (media_files→album_variants)
    AND its phantom album (album_tracks), so the shorter phantom bridge alone wouldn't
    drop 87% of owned links."""
    Lt = L.table.split()[0]

    def one(c):
        frm, corr = _subquery_from(_route(Lt, atom, c))
        return f"SELECT {L.pk} AS lid FROM {frm} WHERE {corr.replace(atom.pk, 'm.id')}"

    if corpus == "all" and _route(Lt, atom, "owned") != _route(Lt, atom, "phantom"):
        return f"JOIN LATERAL ({one('owned')} UNION {one('phantom')}) lk ON true"
    return f"JOIN LATERAL ({one(corpus)}) lk ON true"


def _model_ready(key: str) -> bool:
    import model_cache
    return model_cache.is_loaded(key)


def _kick_model(key: str) -> None:
    import model_cache
    from routers.discovery import _enrichment_loader, _clap_loader, _lyrics_loader
    factory = {"enrichment": _enrichment_loader, "clap": _clap_loader,
               "lyrics": _lyrics_loader}.get(key)
    if factory:
        model_cache.kick_load(key, factory)


def _encode_bge(q: str) -> str:
    from search import _encode_enrichment_query, _to_vector_param
    return _to_vector_param(_encode_enrichment_query(q))


def _encode_clap(q: str) -> str:
    import model_cache
    from search import _to_vector_param

    def _load():
        from embeddings import AudioEmbeddingGenerator
        g = AudioEmbeddingGenerator()
        g.load_model()
        return g
    return _to_vector_param(model_cache.get_model("clap", _load).text_to_embedding(q))


def _encode_lyrics(q: str) -> str:
    import model_cache
    from search import _to_vector_param

    def _load():
        from lyrics_embeddings import LyricsEmbeddingGenerator
        g = LyricsEmbeddingGenerator()
        g.load_model()
        return g
    return _to_vector_param(model_cache.get_model("lyrics", _load).query_to_embedding(q))


def _bind_params(tools, active: dict) -> dict:
    """Translate active tool values into SQL params. TODO(step 3+): bpm ranges,
    corpus. Vector params (qvec/qclap) are encoded once per text query."""
    from transliterate import latinize

    p: dict = {}
    for tool in tools:
        v = active.get(tool.key)
        if v in (None, "", "any", []):
            continue
        if tool.key == "text":
            q = str(v)[:255]
            ql = (latinize(q) or q)[:255]
            p["q"] = q
            p["ql"], p["qlpfx"] = ql, ql + "%"
            if _model_ready("enrichment"):
                p["qvec"] = _encode_bge(q)   # bio source (BGE-M3, multilingual) — only if warm
            if _model_ready("clap"):
                # CLAP's text encoder is English-only — non-Latin queries score
                # noise-level here until the planned LOCAL translation model
                # lands; the multilingual BGE sources (bio, lyrics) carry them.
                p["qclap"] = _encode_clap(q)
            if _model_ready("lyrics"):
                p["qlyr"] = _encode_lyrics(q)  # lyrics source (BGE-M3, multilingual)
        elif tool.key in ("gender", "vocalist", "key", "mode"):
            p[tool.key] = v
        elif tool.key == "bpm":
            p["bpm_min"], p["bpm_max"] = v   # (min, max)
        elif tool.key == "genre":
            p["genre"] = list(v) if isinstance(v, (list, tuple)) else [v]
        elif tool.key == "moods":
            p["moods"] = list(v) if isinstance(v, (list, tuple)) else [v]
        elif tool.key == "instruments":
            exp = tool.sources[0].expand
            p["instruments"] = exp(v) if exp else (list(v) if isinstance(v, (list, tuple)) else [v])
        elif tool.key == "energy":
            # energy_db buckets (mirror routers/discovery.ENERGY_BUCKETS)
            buckets = {"low": (-100.0, -25.0), "mid": (-25.0, -15.0), "high": (-15.0, 0.0)}
            p["energy_lo"], p["energy_hi"] = buckets.get(v, (-100.0, 0.0))
        elif tool.key == "sound":
            if _model_ready("clap"):
                p["qclap"] = _encode_clap(str(v)[:255])
        elif tool.key == "lyrics":
            if _model_ready("lyrics"):
                p["qlyr"] = _encode_lyrics(str(v)[:255])
    return p


_INSTRUMENT_GROUPS = {
    "piano": ["piano", "electric piano", "keyboard (musical)"],
    "guitar": ["guitar", "acoustic guitar", "plucked string instrument"],
    "electric guitar": ["electric guitar"],
    "bass": ["bass guitar", "electric bass", "double bass"],
    "drums": ["drum", "drum kit", "drum machine", "percussion", "bass drum",
              "snare drum", "cymbal", "hi-hat", "tabla", "tambourine"],
    "strings": ["violin, fiddle", "cello", "bowed string instrument"],
    "orchestra": ["orchestra"],
    "synth": ["synthesizer", "sampler"],
    "brass": ["brass instrument", "trumpet", "trombone", "french horn", "tuba"],
    "saxophone": ["saxophone"],
}


def _expand_instruments(broad: list) -> list:
    """Broad instrument chip → AST/PaSST raw tag set for the JSONB ?| any-of match."""
    out: list = []
    for b in broad:
        out.extend(_INSTRUMENT_GROUPS.get(b.lower(), [b.lower()]))
    return list(dict.fromkeys(out))
