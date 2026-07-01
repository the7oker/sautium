"""Discovery search engine — tool-driven composable query builder.

Replaces routers/discovery.py, the filter logic in search.py (_filter_clauses /
_apply_filters), and the MCP search tools with ONE builder. See
docs/design/DISCOVERY-SEARCH-ENGINE.md (§Tools/§Bridges).

Model:
  Tool   = one UI control (text search, Vocalist, Gender, Energy, Instruments…).
           Declares its sources and the entity targets its results can resolve to.
  Source = one signal with a score contract: is_gate sources are binary filters
           (WHERE, no rank); the rest are relevance sources scored to [0,1] and
           normalized per-source (calibrated min-max) so heterogeneous signals
           are comparable and can be summed for one ORDER BY.
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


ENTITIES: dict[str, EntityDef] = {
    "artist": EntityDef("artist", "a.id", "a.name", "a.name_latin", "artists a",
                        "(SELECT COUNT(*) FROM track_artists ta "
                        "WHERE ta.artist_id=a.id AND ta.role='primary') DESC"),
    "album":  EntityDef("album", "al.id", "al.title", "al.title_latin", "albums al",
                        "al.release_year DESC NULLS LAST, al.title"),
    "track":  EntityDef("track", "t.id", "t.title", "t.title_latin", "tracks t",
                        "t.title"),
}


# ── Sources & tools ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Source:
    key: str
    table: str                 # table the score_sql reads (bridge routing anchor)
    score_sql: str             # raw score in [0,1] (relevance) OR a boolean predicate (gate)
    is_gate: bool = False      # binary filter: pure WHERE, contributes no rank
    floor: float = 0.0         # relevance threshold — also the WHERE cutoff (calibrate: Phase 2)
    ceil: float = 1.0          # "strong match" — norm caps here (calibrate: Phase 2)
    weight: float = 1.0        # importance in the cross-tool sum
    needs_join: frozenset = frozenset()
    expand: Optional[Callable[[Any], Any]] = None


@dataclass(frozen=True)
class Tool:
    key: str
    sources: tuple             # tuple[Source, ...]
    targets: tuple             # entity keys this tool's results can resolve to


TOOLS: dict[str, Tool] = {
    # Multi-source, multi-target relevance. Each source scored + normalized; the
    # tool's contribution is GREATEST(norm over its sources) — name OR bio OR ….
    "text": Tool("text", targets=("artist", "album", "track", "genre"), sources=(
        Source("artist_name", "artists",
               "GREATEST(similarity(a.name_latin,:ql), "
               "CASE WHEN a.name_latin LIKE :qlpfx THEN 0.85 ELSE 0 END)",
               floor=0.3, ceil=1.0, weight=1.0),
        Source("artist_bio", "artist_bio_embeddings", "1-(abe.vector <=> CAST(:qvec AS vector))",
               floor=0.5, ceil=0.85, weight=0.7,
               needs_join=frozenset({"artist_bio_embeddings"})),
        Source("album_title", "albums",
               "GREATEST(similarity(al.title_latin,:ql), "
               "CASE WHEN al.title_latin LIKE :qlpfx THEN 0.85 ELSE 0 END)",
               floor=0.3, ceil=1.0, weight=1.0),
        Source("track_title", "tracks",
               "GREATEST(similarity(t.title_latin,:ql), "
               "CASE WHEN t.title_latin LIKE :qlpfx THEN 0.85 ELSE 0 END)",
               floor=0.3, ceil=1.0, weight=1.0),
        Source("clap", "embeddings", "1-(e.vector <=> CAST(:qclap AS vector))",
               floor=0.25, ceil=0.45, weight=0.8,   # CLAP text→audio: low absolute scale
               needs_join=frozenset({"embeddings"})),
    )),
    # Binary gates (artist-level).
    "vocalist": Tool("vocalist", targets=("artist",), sources=(
        Source("vocalist", "artists", "a.is_vocalist = :vocalist", is_gate=True),)),
    "gender": Tool("gender", targets=("artist",), sources=(
        Source("gender", "artists", "a.gender = :gender", is_gate=True),)),
    # Track-level. Energy is smooth (distance to bucket midpoint → 0..1), not a gate.
    "energy": Tool("energy", targets=("track",), sources=(
        Source("energy", "audio_features",
               "GREATEST(0, 1 - abs(af.energy_db - :energy_mid)/:energy_span)",
               floor=0.0, ceil=1.0, needs_join=frozenset({"audio_features"})),)),
    "instruments": Tool("instruments", targets=("track",), sources=(
        Source("instruments", "audio_features", "af.instruments ?| :instruments",
               is_gate=True, needs_join=frozenset({"audio_features"}),
               expand=lambda v: _expand_instruments(v)),)),
    "moods": Tool("moods", targets=("track",), sources=(
        Source("moods", "audio_features", "af.moods ?| :moods", is_gate=True,
               needs_join=frozenset({"audio_features"})),)),
    "bpm": Tool("bpm", targets=("track",), sources=(
        Source("bpm", "audio_features", "af.bpm BETWEEN :bpm_min AND :bpm_max",
               is_gate=True, needs_join=frozenset({"audio_features"})),)),
    "key": Tool("key", targets=("track",), sources=(
        Source("key", "audio_features", "af.key = :key", is_gate=True,
               needs_join=frozenset({"audio_features"})),)),
    # Album-level.
    "genre": Tool("genre", targets=("track", "album", "artist"), sources=(
        Source("genre", "genres", "g.name = ANY(:genre)", is_gate=True,
               needs_join=frozenset({"album_genres", "genres"})),)),
    "year": Tool("year", targets=("album", "track"), sources=(
        Source("year", "albums", "al.release_year BETWEEN :year_from AND :year_to",
               is_gate=True),)),
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
    Edge("artists", "artist_bio_embeddings", "abe.artist_id = a.id"),
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


def _exists_gate(src: Source, entity: EntityDef, corpus: str) -> str:
    """A below-target gate becomes EXISTS over the bridge path (e.g. genre on a
    track target → 'track has an album with this genre')."""
    frm, corr = _subquery_from(_route(src.table, entity, corpus))
    return f"EXISTS (SELECT 1 FROM {frm} WHERE {corr} AND {src.score_sql})"


def _lateral_relevance(src: Source, entity: EntityDef, corpus: str) -> tuple[str, str, str]:
    """A below/child relevance source → LEFT JOIN LATERAL over the bridge taking
    MAX(norm), so 1:N (bio chunks) and 1:1 (clap/energy) both work. Returns
    (lateral_join_sql, score_ref, floor_predicate)."""
    frm, corr = _subquery_from(_route(src.table, entity, corpus))
    alias = f"{src.key}_j"
    join = (f"LEFT JOIN LATERAL (SELECT MAX({_norm_expr(src)}) AS s "
            f"FROM {frm} WHERE {corr}) {alias} ON true")
    return join, f"COALESCE({alias}.s, 0)", f"{alias}.s > 0"


# ── Score normalization ─────────────────────────────────────────────────────

def _norm_expr(src: Source) -> str:
    """Calibrated per-source min-max in SQL: clamp((raw - floor)/(ceil - floor), 0, 1).
    Makes 0.5 mean the same across sources so scores can be summed for ORDER BY."""
    span = src.ceil - src.floor
    return f"LEAST(1.0, GREATEST(0.0, (({src.score_sql}) - {src.floor}) / {span}))"


# ── Builder ─────────────────────────────────────────────────────────────────

def build(active: dict, query: dict, corpus: str = "all", limit: int = 20) -> dict:
    """active = {tool_key: value}. Returns {target: (sql, params)} for every target
    the active tools resolve to (union of their targets, minus corpus-impossible ones).

    Per target: gather each active source, route its table→target via _route (dedup
    joins), assemble WHERE (gates + relevance floors) and the SELECT/ORDER from the
    per-tool GREATEST(norm) summed with weights. TODO: assembly, _route, corpus
    two-path handling, aggregable HAVING, cover/media surfacing.
    """
    tools = [TOOLS[k] for k in active if k in TOOLS]
    targets = _feasible_targets(tools, corpus)
    return {tk: _build_for_target(ENTITIES[tk], tools, active, query, corpus, limit)
            for tk in targets}


def _feasible_targets(tools: list, corpus: str) -> set:
    """Union of the tools' targets. TODO: drop targets a gate can't reach under the
    corpus (e.g. an owned-only file source with corpus='phantom')."""
    out: set = set()
    for t in tools:
        out |= set(t.targets)
    return out & set(ENTITIES)   # only entities we can build (genre target: TODO step 2+)


def _build_for_target(entity, tools, active, query, corpus, limit):
    """Step 1 (same-table): only sources whose table IS the target's table — no
    bridge, no corpus branch yet. Gates → WHERE; relevance → normalized score,
    summed with weights for ORDER BY; a row must clear at least one relevance
    floor. TODO(step 2+): _route bridges (bio/clap/genre/energy), corpus tails."""
    entity_table = entity.table.split()[0]
    gates: list[str] = []
    rel_terms: list[str] = []      # weighted GREATEST(norm) per tool
    rel_floors: list[str] = []     # per relevance source: raw >= floor
    joins: set = set()
    lateral_joins: list[str] = []

    for tool in tools:
        if entity.key not in tool.targets:
            continue
        norms: list[str] = []
        weight = 0.0
        for src in tool.sources:
            same = src.table == entity_table
            if src.is_gate:
                if same:
                    gates.append(src.score_sql)
                    joins |= src.needs_join
                else:
                    gates.append(_exists_gate(src, entity, corpus))   # below-target gate → EXISTS
            elif same:
                norms.append(_norm_expr(src))
                rel_floors.append(f"({src.score_sql}) >= {src.floor}")
                weight = max(weight, src.weight)
            else:                  # bridged relevance → LEFT JOIN LATERAL (MAX norm)
                lat, ref, fl = _lateral_relevance(src, entity, corpus)
                lateral_joins.append(lat)
                norms.append(ref)
                rel_floors.append(fl)
                weight = max(weight, src.weight)
        if norms:
            rel_terms.append(f"{weight} * GREATEST({', '.join(norms)})")

    where = list(gates)
    if rel_floors:                 # at least one relevance source clears its floor
        where.append("(" + " OR ".join(rel_floors) + ")")

    score = " + ".join(rel_terms) if rel_terms else "NULL"
    order = f"({score}) DESC, {entity.default_order}" if rel_terms else entity.default_order
    join_sql = " ".join(sorted(joins) + lateral_joins)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    sql = (f"SELECT {entity.pk} AS id, {entity.name_col} AS name, ({score}) AS score "
           f"FROM {entity.table} {join_sql}{where_sql} "
           f"ORDER BY {order} LIMIT {int(limit)}")
    return sql, _bind_params(tools, active)


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
            p["ql"], p["qlpfx"] = ql, ql + "%"
            p["qvec"] = _encode_bge(q)      # bio source (BGE-M3)
            p["qclap"] = _encode_clap(q)    # CLAP source (text→audio)
        elif tool.key in ("gender", "vocalist", "key"):
            p[tool.key] = v
        elif tool.key == "genre":
            p["genre"] = list(v) if isinstance(v, (list, tuple)) else [v]
        elif tool.key == "moods":
            p["moods"] = list(v) if isinstance(v, (list, tuple)) else [v]
        elif tool.key == "instruments":
            exp = tool.sources[0].expand
            p["instruments"] = exp(v) if exp else (list(v) if isinstance(v, (list, tuple)) else [v])
        elif tool.key == "energy":
            # (mid, span) in energy_db; TODO calibrate real buckets (Phase 2)
            buckets = {"low": (-30.0, 10.0), "mid": (-20.0, 8.0), "high": (-10.0, 8.0)}
            p["energy_mid"], p["energy_span"] = buckets.get(v, (-20.0, 15.0))
    return p


def _expand_instruments(broad: list) -> list:
    """Broad instrument chip → AST/PaSST tag set (moves here from
    routers/discovery.INSTRUMENT_GROUPS when that endpoint is absorbed)."""
    raise NotImplementedError("instrument group expansion")
