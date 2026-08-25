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

Composition is ATOM-CENTRIC (see build()): gates AND on the lowest active level,
relevance OR via indexed retrieve→rerank, higher targets = own-level identity +
AVG roll-up of the characteristic channel. Vector floors/ceils calibrated from
live score distributions 2026-07-02.
"""
from dataclasses import dataclass
from typing import Any, Callable, Optional

import track_similarity
from uuid_utils import artist_uuid

_VA_ID = str(artist_uuid("Various Artists"))


# ── Entities ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EntityDef:
    key: str
    pk: str
    name_col: str
    table: str          # own table + alias, e.g. "artists a"
    default_order: str  # browse-mode ordering (no relevance signal active)
    surface: str = ""   # extra SELECT columns for tiles (is_owned, cover_id, media_file_id, …)


ENTITIES: dict[str, EntityDef] = {
    "artist": EntityDef("artist", "a.id", "a.name", "artists a",
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
    "album":  EntityDef("album", "al.id", "al.title", "albums al",
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
    # Genre is a CATEGORY level above the containment chain: reachable as a
    # roll-up target from any atom ("romantic saxophone" → smooth jazz), plus two
    # own-level identity sources (name trigram, description vector). Its albums
    # live on the genre page — search only surfaces the genre itself (Valerii's
    # entity-scoped rule).
    "genre":  EntityDef("genre", "g.id", "g.name", "genres g",
                        "(SELECT COUNT(*) FROM album_genres ag WHERE ag.genre_id=g.id) DESC",
                        surface=", (SELECT COUNT(DISTINCT ag.album_id) FROM album_genres ag "
                        "JOIN album_variants av ON av.album_id=ag.album_id "
                        "WHERE ag.genre_id=g.id) AS album_count, "
                        "EXISTS (SELECT 1 FROM album_genres ag JOIN album_variants av "
                        "ON av.album_id=ag.album_id WHERE ag.genre_id=g.id) AS is_owned"),
    "track":  EntityDef("track", "t.id", "t.title", "tracks t",
                        "t.title",
                        surface=", (SELECT a.name FROM track_artists ta JOIN artists a ON a.id=ta.artist_id "
                        "WHERE ta.track_id=t.id AND ta.role='primary' LIMIT 1) AS artist, "
                        "EXISTS (SELECT 1 FROM media_files mf WHERE mf.track_id=t.id) AS is_owned, "
                        "(SELECT mf.id FROM media_files mf WHERE mf.track_id=t.id "
                        "ORDER BY mf.is_analysis_source DESC LIMIT 1) AS media_file_id, "
                        "(SELECT mf.cover_id::text FROM media_files mf WHERE mf.track_id=t.id "
                        "AND mf.cover_id IS NOT NULL LIMIT 1) AS cover_id, "
                        "(SELECT al.cover_url FROM album_tracks atr JOIN albums al ON al.id=atr.album_id "
                        "WHERE atr.track_id=t.id AND al.cover_url IS NOT NULL LIMIT 1) AS cover_url, "
                        # album / year / duration fall back to the tracklist row: a
                        # not-owned track has no media_file to read them from, and a
                        # result tile with no album and no length reads as broken.
                        "COALESCE((SELECT mf.duration_seconds FROM media_files mf WHERE mf.track_id=t.id LIMIT 1), "
                        "(SELECT atr.length_ms/1000.0 FROM album_tracks atr WHERE atr.track_id=t.id "
                        "AND atr.length_ms IS NOT NULL LIMIT 1)) AS duration_seconds, "
                        "COALESCE((SELECT al.title FROM media_files mf JOIN album_variants av ON av.id=mf.album_variant_id "
                        "JOIN albums al ON al.id=av.album_id WHERE mf.track_id=t.id LIMIT 1), "
                        "(SELECT al.title FROM album_tracks atr JOIN albums al ON al.id=atr.album_id "
                        "WHERE atr.track_id=t.id ORDER BY (al.cover_url IS NOT NULL) DESC, al.id LIMIT 1)) AS album, "
                        "COALESCE((SELECT al.release_year FROM media_files mf JOIN album_variants av ON av.id=mf.album_variant_id "
                        "JOIN albums al ON al.id=av.album_id WHERE mf.track_id=t.id LIMIT 1), "
                        "(SELECT al.release_year FROM album_tracks atr JOIN albums al ON al.id=atr.album_id "
                        "WHERE atr.track_id=t.id AND al.release_year IS NOT NULL ORDER BY al.id LIMIT 1)) AS year"),
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
    floor: float = 0.0         # relevance threshold — also the WHERE cutoff (calibrated 2026-07-02)
    ceil: float = 1.0          # "strong match" — norm caps here (calibrated 2026-07-02)
    weight: float = 1.0        # importance in the cross-tool sum
    model: Optional[str] = None   # model_cache key this source needs; gracefully skipped if cold
    level: str = ""            # entity level (track/album/artist); default derived from .table
    retrieve: str = ""         # index-friendly retrieve-branch predicate; default (score_sql) >= floor
    retrieve_order: str = ""   # retrieve-branch ORDER BY override (carries its own direction).
                               # For sources whose score_sql is expensive (seed's chamfer): order
                               # the branch by something cheap instead of computing the score for
                               # every member row.
    knn: str = ""              # HNSW-served distance ORDER expr (e.g. "es.vector <=> CAST(:q AS vector)"):
                               # the retrieve branch becomes inner-KNN-first — a bare
                               # `ORDER BY vector <=> q LIMIT n` uses the index, the wrapped
                               # `1-(...)` score form never does. pgvector 0.5 returns at most
                               # hnsw.ef_search rows, so executors SET LOCAL hnsw.ef_search=1000.
    knn_limit: int = 1000      # inner-KNN overscan; must stay <= the executor's ef_search.
                               # Sources with an expensive per-row score (seed's chamfer
                               # subquery) trim it to bound the rerank cost.
    expand: Optional[Callable[[Any], Any]] = None


@dataclass(frozen=True)
class Tool:
    key: str
    sources: tuple             # tuple[Source, ...]
    broad: bool = False        # non-selective gate (mode ~51%): browse roll-ups above the
                               # atom are skipped when every active tool is broad — ranking
                               # by count then degenerates to catalog size (corr 0.94)


# Seed (audio↔audio) two-tier score over an `embeddings e` row — the same
# formula as track_similarity.similar_tracks: segment chamfer (for each seed
# 10s window, the candidate's best window, averaged) minus the Last.fm
# artist-tag bonus (W_TAG × weighted Jaccard of the two artists' tag
# profiles — the one measured signal that hits GENRE; see
# track_similarity.py, including why bios/genre-tags/BPM/energy/instruments
# were all rejected). Expressed as correlated subqueries so it fits the
# engine's per-row score grammar. Mean↔mean was dropped 2026-07-13: the
# post-mean-flip space is concentrated and its ordering is near-noise.
# Mean-KNN survives only as the seed source's retrieve branch (knn=); the
# seed_kin source is the second recall arm (see the Tool below).
_SEED_TAG_JAC = """(SELECT SUM(LEAST(st.w, COALESCE(cat.weight, 1)::numeric))
             / NULLIF((SELECT COALESCE(SUM(COALESCE(at3.weight, 1)), 0)::numeric
                       FROM artist_tags at3
                       JOIN track_artists sta3 ON sta3.track_id = CAST(:seed_tid AS uuid)
                           AND sta3.role = 'primary' AND at3.artist_id = sta3.artist_id)
                      + (SELECT COALESCE(SUM(COALESCE(at4.weight, 1)), 0)::numeric
                         FROM artist_tags at4
                         JOIN track_artists cta4 ON cta4.track_id = e.track_id
                             AND cta4.role = 'primary' AND at4.artist_id = cta4.artist_id)
                      - SUM(LEAST(st.w, COALESCE(cat.weight, 1)::numeric)), 0)
        FROM (SELECT at5.tag_id, MAX(COALESCE(at5.weight, 1))::numeric AS w
              FROM artist_tags at5
              JOIN track_artists sta5 ON sta5.track_id = CAST(:seed_tid AS uuid)
                  AND sta5.role = 'primary' AND at5.artist_id = sta5.artist_id
              GROUP BY at5.tag_id) st
        JOIN track_artists cta ON cta.track_id = e.track_id AND cta.role = 'primary'
        JOIN artist_tags cat ON cat.tag_id = st.tag_id AND cat.artist_id = cta.artist_id)"""

_SEED_SCORE = f"""1 - ((SELECT avg(w.best) FROM (
        SELECT min(ss.vector <=> cs.vector) AS best
        FROM embedding_segments ss
        JOIN embeddings se ON se.id = ss.embedding_id
        CROSS JOIN embedding_segments cs
        WHERE se.track_id = CAST(:seed_tid AS uuid)
          AND cs.embedding_id = e.id
        GROUP BY ss.segment_index) w)
    - {track_similarity.W_TAG} * COALESCE({_SEED_TAG_JAC}, 0))"""

# seed_kin retrieve predicate: candidate's primary artist is the seed's
# primary artist or one of its Last.fm similars — the second recall arm
# (mean-KNN misses the perceptual neighbourhood: Sade's own Sweetest Taboo
# sat at mean-rank 678 for a Smooth Operator seed).
_SEED_KIN_PRED = """EXISTS (
    SELECT 1 FROM track_artists kta
    JOIN (SELECT sta.artist_id FROM track_artists sta
          WHERE sta.track_id = CAST(:seed_tid AS uuid) AND sta.role = 'primary'
          UNION
          SELECT sa.similar_artist_id FROM similar_artists sa
          JOIN track_artists sta2 ON sta2.track_id = CAST(:seed_tid AS uuid)
              AND sta2.role = 'primary' AND sa.artist_id = sta2.artist_id) kk
        ON kk.artist_id = kta.artist_id
    WHERE kta.track_id = e.track_id AND kta.role = 'primary')"""


TOOLS: dict[str, Tool] = {
    # SCOPED text search (2026-07-18): the old mega-tool blended lexical +
    # three vector channels on every keystroke — expensive (3 GPU encodes +
    # segment KNN for a plain artist-name query) and unexplainable ("sade"
    # pulled sad-lyrics tracks via BGE). The UI's "Search in" scope now maps
    # q to exactly ONE relevance tool: text (names & titles, the default),
    # bio, sound, or lyrics.
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
               retrieve="(ana.alias_latin % :ql OR ana.alias_latin LIKE :qlpfx)"),
        Source("album_title", "albums",
               "GREATEST(similarity(al.title_latin,:ql), "
               "CASE WHEN al.title_latin LIKE :qlpfx THEN 0.85 ELSE 0 END)",
               targets=("album",), floor=0.3, ceil=1.0, weight=1.0,
               retrieve="(al.title_latin % :ql OR al.title_latin LIKE :qlpfx)"),
        # album/artist in targets = the lexical CONTAINER lift: "Casanova" surfaces
        # Zorya (the album that HAS the track) and Floex. Genre stays out — "genres
        # having a track with this name" is noise. Containers ride the MAX channel
        # (s_lex), not the shrunk average: one exact-titled track IS full proof.
        Source("track_title", "tracks",
               "GREATEST(similarity(t.title_latin,:ql), "
               "CASE WHEN t.title_latin LIKE :qlpfx THEN 0.85 ELSE 0 END)",
               targets=("track", "album", "artist"), floor=0.3, ceil=1.0, weight=1.0,
               retrieve="(t.title_latin % :ql OR t.title_latin LIKE :qlpfx)"),
        # Genre lexical identity — "Trip-Hop" typed as a name lands here.
        Source("genre_name", "genres",
               "GREATEST(similarity(g.name,:ql), "
               "CASE WHEN lower(g.name) LIKE :qlpfx THEN 0.85 ELSE 0 END)",
               targets=("genre",), level="genre", floor=0.3, ceil=1.0, weight=1.0,
               retrieve="(g.name % :ql OR lower(g.name) LIKE :qlpfx)"),
    )),
    # "Artist bios · AI" scope: describe an artist in words. Floor/ceil
    # calibrated 2026-07-02 (probe: floor ≈ corpus p90, ceil ≈ top-10 mean —
    # the old 0.85 ceil sat above the source's best producible score, top1 .59).
    "bio": Tool("bio", sources=(
        Source("artist_bio", "artist_bio_embeddings", "1-(abe.vector <=> CAST(:qvec AS vector))",
               targets=("artist",), floor=0.47, ceil=0.57, weight=1.0, model="enrichment"),)),
    # Binary gates (artist-level).
    "vocalist": Tool("vocalist", sources=(
        Source("vocalist", "artists", "a.is_vocalist = :vocalist", is_gate=True),)),
    "gender": Tool("gender", sources=(
        Source("gender", "artists", "a.gender = :gender", is_gate=True),)),
    # Track-level gates. mode/danceable/energy are BROAD (mode splits the library
    # ~50/50) — see Tool.broad.
    "energy": Tool("energy", broad=True, sources=(
        Source("energy", "audio_features", "af.energy_db BETWEEN :energy_lo AND :energy_hi",
               is_gate=True),)),
    # instruments stores the RAW top-20 score distribution (no write-time
    # cutoff) — presence is a read-time decision against per-label thresholds
    # (ensemble_instruments.INSTRUMENT_THRESHOLDS). `?|` alone would match
    # noise-level scores; it stays only as the GIN-indexed retrieve branch in
    # front of the score check.
    "instruments": Tool("instruments", sources=(
        Source("instruments", "audio_features",
               "(af.instruments ?| :instruments AND EXISTS "
               "(SELECT 1 FROM jsonb_each_text(CAST(:instruments_thr AS jsonb)) th "
               "WHERE (af.instruments->>th.key)::float >= th.value::float))",
               is_gate=True,
               expand=lambda v: _expand_instruments(v)),)),
    "moods": Tool("moods", sources=(
        Source("moods", "audio_features", "af.moods ?| :moods", is_gate=True),)),
    "bpm": Tool("bpm", sources=(
        Source("bpm", "audio_features", "af.bpm BETWEEN :bpm_min AND :bpm_max",
               is_gate=True),)),
    "key": Tool("key", sources=(
        Source("key", "audio_features", "af.key = :key", is_gate=True),)),
    "mode": Tool("mode", broad=True, sources=(
        Source("mode", "audio_features", "af.mode = :mode", is_gate=True),)),
    "danceable": Tool("danceable", broad=True, sources=(
        Source("danceable", "audio_features", "af.danceability >= 0.5", is_gate=True),)),
    # Album-level.
    "genre": Tool("genre", sources=(
        Source("genre", "genres", "g.name = ANY(:genre)", is_gate=True),)),
    "year": Tool("year", sources=(
        Source("year", "albums", "al.release_year BETWEEN :year_from AND :year_to",
               is_gate=True),)),
    # Current-track context ("Similar to now playing"): the two-tier audio↔audio
    # score (_SEED_SCORE — same formula/weights as radio and the Now Playing
    # shelf, see track_similarity.py). Characteristic ⇒ rolls up: similar
    # tracks / albums / artists / genres from one checkbox, AND-composable with
    # every gate ("similar + Trip-Hop only"). Retrieve = mean-KNN (knn=, recall
    # only — mean ordering is near-noise) UNION the seed_kin arm (same score,
    # kin-membership retrieve ordered by the cheap mean distance — computing
    # chamfer for every kin row just to pick the branch top-K would be the
    # expensive way around). Rerank = chamfer − tag bonus. Floor .76 from the
    # 2026-07-23 pure-chamfer probe (sparse Berlin-school corner p90 .785);
    # ceil .98 sits above the tag-boosted top (chamfer .10 − bonus .08 →
    # sim ≈ .977 for the seed artist's own tracks) so near-ties never clamp
    # into 1.00. The self-exclusion gate keeps the seed out of the matched
    # set, so its own album/artist rank only via their OTHER similar content.
    "seed": Tool("seed", sources=(
        Source("seed", "embeddings", _SEED_SCORE,
               targets=("track", "album", "artist", "genre"),
               floor=0.76, ceil=0.98, weight=1.0,
               knn="e.vector <=> CAST(:qseed AS vector)",
               knn_limit=track_similarity.POOL),
        Source("seed_kin", "embeddings", _SEED_SCORE,
               targets=("track", "album", "artist", "genre"),
               floor=0.76, ceil=0.98, weight=1.0,
               retrieve=_SEED_KIN_PRED,
               retrieve_order="(e.vector <=> CAST(:qseed AS vector)) + 0"),
        Source("seed_not_self", "tracks", "t.id <> CAST(:seed_tid AS uuid)",
               is_gate=True),
    )),
    # Artist filter (AND): typeahead-picked artist ids — "similar tracks, but
    # only by these artists". Distinct from text relevance, which ranks and
    # never narrows.
    "artist": Tool("artist", sources=(
        Source("artist_sel", "artists", "a.id = ANY(CAST(:artist_ids AS uuid[]))",
               is_gate=True),)),
    # "Sound · AI" scope: describe the music in words. CLAP text→audio works
    # against SEGMENTS (10s windows), not track means: normalized means collapse
    # toward the corpus centroid (text-query cosines span ~0.05 across 37k
    # tracks) and HNSW breaks down in that space. Retrieve = segment KNN (knn=),
    # score = MAX over the track's segments. Floors from the 2026-07-05
    # segment-MAX probe: floor = corpus p90 (.42); ceil ABOVE the probe's max
    # top-1 (.726) — a dense space clamps dozens of rows into 1.00 ties
    # otherwise. genre_desc rides along (BGE, own :qgdesc param): a curated
    # genre description IS a music description — "saxophone" reaches Jazz.
    "sound": Tool("sound", sources=(
        Source("sound", "embedding_segments", "1-(es.vector <=> CAST(:qclap AS vector))",
               targets=("track", "album", "artist", "genre"),
               floor=0.42, ceil=0.74, weight=1.0, model="clap",
               knn="es.vector <=> CAST(:qclap AS vector)"),
        Source("genre_desc", "genre_desc_embeddings", "1-(gde.vector <=> CAST(:qgdesc AS vector))",
               targets=("genre",), level="genre", floor=0.47, ceil=0.57, weight=0.7,
               model="enrichment"),)),
    "lyrics": Tool("lyrics", sources=(
        Source("lyrics", "lyrics_embeddings", "1-(le.vector <=> CAST(:qlyr AS vector))",
               targets=("track", "album", "artist", "genre"),
               floor=0.47, ceil=0.62, weight=1.0, model="lyrics"),)),
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
    Edge("embeddings", "embedding_segments", "es.embedding_id = e.id"),
    Edge("tracks", "lyrics_embeddings", "le.track_id = t.id"),
    Edge("artists", "artist_bio_embeddings", "abe.artist_id = a.id"),
    Edge("artists", "artist_name_aliases", "ana.artist_id = a.id"),
    Edge("genres", "genre_desc_embeddings", "gde.genre_id = g.id"),
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
    "embedding_segments": "es",
    "genre_desc_embeddings": "gde",
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
    "genre":  "EXISTS (SELECT 1 FROM album_genres ag JOIN album_variants av "
              "ON av.album_id=ag.album_id WHERE ag.genre_id=g.id)",
}


# Tie-break tail for EVERY ordering that a saturating score feeds. Lexical identity
# SATURATES: an exact title clamps to the source ceil, so 1660 tracks named "intro"
# and 17 named "Ursa Major" all score exactly 1.0 and `score DESC, name` has no key
# left — the emitted rows were whatever the plan produced, and it changed with LIMIT.
# Ownership is the only signal that survives the saturation (track_stats covers owned
# rows only, 36k of 3M) and it is the right one: at equal relevance a lossless file
# you own beats an MB-dump stub you cannot play. It stays a TIE-BREAK, never a score
# term — as score it would let a weak owned match outrank a strong phantom one and
# would skew _DOMINANCE_CUT's ratio. The pk tail makes the order TOTAL, without which
# OFFSET pages duplicate and skip rows.
#
# This MUST also tail the retrieve branches, not just the final sort: they cut their
# own top-K off the same saturated score, so an arbitrary cut there discards owned
# rows before ranking ever sees them (5 of 33 owned "intro" tracks survived) and
# hands two executions of one query different candidate sets. Measured cost of the
# EXISTS in that ORDER BY, on the library's worst case (6462 candidates): 489→502ms.
_TIE_BREAK = "is_owned DESC, name"          # by output-column name: the final sorts


def _tie_break(entity: EntityDef) -> str:
    """The same keys against `entity`'s own table, for the scopes that project no
    is_owned/name column of their own (the retrieve branches)."""
    return f"({_OWNED_GUARD[entity.key]}) DESC, {entity.name_col}, {entity.pk}"


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
    "embedding_segments": "track",
    "lyrics_embeddings": "track", "album_tracks": "track", "media_files": "track",
    "track_artists": "track",
}
# atom = the MIN (finest) rank. Genre tops the ladder: a category every lower
# level rolls up into — never an atom itself (its gate is album-grain via
# _TABLE_LEVEL["genres"]; only the genre_name/desc relevance sources carry
# level="genre" explicitly).
_LEVEL_RANK = {"track": 0, "album": 1, "artist": 2, "genre": 3}


def _level(src: Source) -> str:
    return src.level or _TABLE_LEVEL[src.table]


# How many rows each relevance source retrieves through its own index before the
# rerank. Also the hard floor on how deep a result list can be paged: the union of
# the branches is all that exists downstream, so nothing beyond it can surface.
RETRIEVE_K = 500


# ── Builder ─────────────────────────────────────────────────────────────────

def build(active: dict, query: dict, corpus: str = "all", limit: int = 20,
          offset: int = 0) -> dict:
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
    {target_key: (sql, params)}.

    `offset` pages the target list. Depth is bounded by RETRIEVE_K: every relevance
    branch retrieves its own top-K, so nothing past K candidates per source exists to
    page into — a page that comes back short IS the end of the list."""
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
        out[lvl] = (_build_atom(atom, tools, active, corpus, limit, offset) if lvl == atom_lvl
                    else _build_higher(atom, ENTITIES[lvl], tools, active, corpus,
                                       limit, offset))
    return out


def _retrieve_branch(src: Source, entity: EntityDef, corpus: str, gates: list, K: int) -> str:
    """One relevance source → SELECT the target pk, top-K via the source's own index,
    gates + corpus pushed down so the top-K is already filtered. same-table: WHERE
    floor ORDER BY score. Bridged: join the source in along the route path and order
    by its score (HNSW for vectors, trigram for names).

    The top-K cut runs on the SAME saturated score the final sort does, so it carries
    the same tie-break (_tie_break) — the pool has to be the deterministic top-K of
    that order, not an arbitrary K of the tied plateau."""
    et = entity.table.split()[0]
    conds = list(gates)
    cg = _corpus_clause(entity, corpus)
    if cg:
        conds.append(cg)
    if src.knn:
        # Inner KNN first — HNSW serves only the bare `ORDER BY vector <=> q`
        # form, never the wrapped score expression. Gates + floor prune the
        # ≤1000-row overscan OUTSIDE the KNN (pgvector 0.5 caps the result at
        # hnsw.ef_search rows — the executor SET LOCALs it to 1000). The bridge
        # path back to the target is walked OUTSIDE the KNN subquery too —
        # each hop is a pkey/unique-index lookup over ≤1000 rows (segments
        # reach tracks via their embeddings row).
        path = _route(src.table, entity, corpus)
        alias = _ALIAS[src.table]
        frm = f"(SELECT * FROM {src.table} {alias} ORDER BY {src.knn} LIMIT {src.knn_limit}) {alias}"
        for i in range(len(path) - 1, 0, -1):
            tbl = path[i - 1][0]
            frm += f" JOIN {tbl} {_ALIAS[tbl]} ON {path[i][1]}"
        frm += f" JOIN {entity.table} ON {path[0][1]}"
        conds.append(f"({src.score_sql}) >= {src.floor}")
        return (f"SELECT {entity.pk} AS id FROM {frm} WHERE {' AND '.join(conds)} "
                f"ORDER BY ({src.score_sql}) DESC, {_tie_break(entity)} LIMIT {K}")
    conds.append(src.retrieve or f"({src.score_sql}) >= {src.floor}")
    if src.table == et:
        frm = entity.table
    else:
        path = _route(src.table, entity, corpus)
        frm = entity.table + " " + " ".join(f"JOIN {t} {_ALIAS[t]} ON {js}" for t, js in path)
    order = src.retrieve_order or f"({src.score_sql}) DESC"
    return (f"SELECT {entity.pk} AS id FROM {frm} WHERE {' AND '.join(conds)} "
            f"ORDER BY {order}, {_tie_break(entity)} LIMIT {K}")


def _matched_core(atom, tools, active, corpus, K=RETRIEVE_K, up_level=None):
    """The retrieve→rerank pieces at the atom level, shared by the atom target and
    every higher aggregation. Gates come from EVERY active tool (AND, promoted to the
    atom via _exists_gate) — NOT filtered by target, which is what let cross-level
    filters (female+vocal on a track atom) silently drop out before. Relevance is
    ATOM-LEVEL SOURCES ONLY (higher-level lexical identity never enters the atom —
    no name-down flooding); each tool yields three score expressions: at_terms
    scores the atom target; up_terms and lex_terms serve exactly ONE higher level —
    `up_level`, the level whose roll-up this build feeds (higher targets each build
    their own matched) — and include only the atom sources that DECLARE that level
    in targets. up_terms = characteristic (model-backed: clap/lyrics) → shrunk-AVG
    roll-up; lex_terms = lexical containment (trigram) → MAX roll-up, because one
    exact-titled track proves the container fully. None (atom target) → no up
    channels at all. Returns (gates, branches, at_terms, up_terms, lex_terms,
    lateral)."""
    et = atom.table.split()[0]
    gates = [src.score_sql if src.table == et else _exists_gate(src, atom, corpus)
             for tool in tools for src in tool.sources if src.is_gate]
    branches: list[str] = []
    at_terms: list[str] = []
    up_terms: list[str] = []
    lex_terms: list[str] = []
    lateral: list[str] = []
    for tool in tools:
        at_norms: list[str] = []
        up_norms: list[str] = []
        lex_norms: list[str] = []
        for src in tool.sources:
            if src.is_gate or _level(src) != atom.key:
                continue
            if src.model and not _source_ready(src, active):
                continue                    # cold-model source skipped, warming kicked
            if src.table == et:
                norm = _norm_expr(src)
            else:
                lat, ref, _ = _lateral_relevance(src, atom, corpus)
                lateral.append(lat)
                norm = ref
            # Weight INSIDE the GREATEST: an exact title (w=1.0) must outrank a
            # lyrics mention (w=0.6) — with an outer max-weight they tied at 1.0.
            at_norms.append(f"{src.weight} * {norm}")
            if up_level and up_level in src.targets:
                (up_norms if src.model else lex_norms).append(f"{src.weight} * {norm}")
            branches.append(_retrieve_branch(src, atom, corpus, gates, K))
        if at_norms:
            at_terms.append(f"GREATEST({', '.join(at_norms)})")
        if up_norms:
            up_terms.append(f"GREATEST({', '.join(up_norms)})")
        if lex_norms:
            lex_terms.append(f"GREATEST({', '.join(lex_norms)})")
    return gates, branches, at_terms, up_terms, lex_terms, lateral


def _relevance_requested(tools) -> bool:
    return any(not s.is_gate for t in tools for s in t.sources)


def _matched_sql(atom, tools, gates, branches, at_terms, up_terms, lex_terms, lateral, corpus):
    """(matched-body, ctes-prefix). matched exposes (id, s_at, s_up, s_lex) at the
    atom level. Browse (no relevance branches) → every gate-matching atom, NULL
    scores (roll-ups then rank by count). Relevance REQUESTED but every source
    skipped (cold models) → an EMPTY matched set, not a full-library browse (a cold
    lyrics-only query must answer nothing + warming, not the catalog A-Z). A channel
    with no eligible source for this build's up_level → constant 0 so its roll-up
    pulls in nothing (a title-only query rolling to GENRE: track_title doesn't
    declare genre, so the genre build sees s_lex = 0)."""
    cg = _corpus_clause(atom, corpus)
    filt = list(gates) + ([cg] if cg else [])
    where = (" WHERE " + " AND ".join(filt)) if filt else ""
    if not branches:
        if _relevance_requested(tools):
            return (f"SELECT {atom.pk} AS id, NULL::float AS s_at, NULL::float AS s_up, "
                    f"NULL::float AS s_lex FROM {atom.table} WHERE false"), ""
        return (f"SELECT {atom.pk} AS id, NULL::float AS s_at, NULL::float AS s_up, "
                f"NULL::float AS s_lex FROM {atom.table}{where}"), ""
    s_at = " + ".join(at_terms)
    s_up = " + ".join(up_terms) if up_terms else "0.0"
    s_lex = " + ".join(lex_terms) if lex_terms else "0.0"
    cand = " UNION ".join(f"({b})" for b in branches)
    body = (f"SELECT {atom.pk} AS id, ({s_at}) AS s_at, ({s_up}) AS s_up, ({s_lex}) AS s_lex "
            f"FROM (SELECT DISTINCT id FROM cand) c JOIN {atom.table} ON {atom.pk}=c.id "
            f"{' '.join(lateral)}{where}")
    return body, f"cand AS ({cand}), "


def _build_atom(atom, tools, active, corpus, limit, offset):
    """Atom target: rerank the bounded candidate set on the full normalized score
    (GREATEST within a tool, weighted sum across tools), gates + corpus in WHERE.
    `scored` is un-paged, so the dominance cut always divides by the GLOBAL max —
    OFFSET applies after it and can't rebase the bar on the page's own leader."""
    gates, branches, at_terms, _, _, lateral = _matched_core(atom, tools, active, corpus)
    cg = _corpus_clause(atom, corpus)
    filt = list(gates) + ([cg] if cg else [])
    where = (" WHERE " + " AND ".join(filt)) if filt else ""
    page = f"LIMIT {int(limit)} OFFSET {int(offset)}"
    if not branches:
        if _relevance_requested(tools):    # every relevance source cold-skipped → honest empty
            return (f"SELECT {atom.pk} AS id, {atom.name_col} AS name, NULL AS score"
                    f"{atom.surface} FROM {atom.table} WHERE false"), _bind_params(tools, active)
        # gates-only / browse: no relevance to tie on, but a mixed corpus still ranks
        # the user's own library first, and the pk keeps the pages disjoint.
        sql = (f"SELECT {atom.pk} AS id, {atom.name_col} AS name, NULL AS score{atom.surface} "
               f"FROM {atom.table}{where} "
               f"ORDER BY is_owned DESC, {atom.default_order}, {atom.pk} {page}")
        return sql, _bind_params(tools, active)
    score = " + ".join(at_terms)
    cand = " UNION ".join(f"({b})" for b in branches)
    sql = (f"WITH cand AS ({cand}), "
           f"scored AS (SELECT {atom.pk} AS id, {atom.name_col} AS name, "
           f"({score}) AS score{atom.surface} "
           f"FROM (SELECT DISTINCT id FROM cand) c JOIN {atom.table} ON {atom.pk}=c.id "
           f"{' '.join(lateral)}{where}) "
           f"SELECT * FROM scored "
           f"WHERE score >= {_DOMINANCE_CUT} * (SELECT MAX(score) FROM scored) "
           f"ORDER BY score DESC, {_TIE_BREAK}, id {page}")
    return sql, _bind_params(tools, active)


# Weight of the roll-up channel vs own-level relevance in a higher target's score.
# Own-level identity (an artist MATCHING "Madonna" by name) must dominate incidental
# roll-up (an artist who merely HAS a track titled "Madonna"). Calibration knob.
_ROLLUP_W = 0.5

# Dominance cut: rows scoring below this fraction of the target's top score are
# noise-in-the-presence-of-a-leader and are dropped — an identity query ("sade")
# otherwise fills the block with the CLAP-junk tail (ambient artists at ~0.15
# next to Sade at 1.0, and tiles don't display scores). With no clear leader
# (a characteristic query where everything sits at 0.2-0.3) the bar drops with
# the max and nothing is cut. Browse rows (NULL scores) are never touched.
_DOMINANCE_CUT = 0.25


def _build_higher(atom, L, tools, active, corpus, limit, offset, K=RETRIEVE_K):
    """Higher target = three channels, by evidence type. Characteristic roll-up:
    shrunk AVG of the matched atoms' s_up (clap/lyrics) — 'the artists whose
    catalogue SOUNDS like this' (Sade for romantic-sax). Lexical containment
    roll-up: MAX of s_lex (track_title on album/artist targets) — 'the album/artist
    that HAS the track you typed' (Casanova → Zorya, Floex); MAX, not the shrunk
    AVG, because one exact-titled track proves containment fully — under the shrink
    a container capped at 0.125 and died to the dominance cut whenever a namesake
    identity leader existed. Own-level: relevance sources whose targets include L
    (artist name/alias/bio on an artist target) scored on the L row directly —
    identity matches, including track-less phantom stubs the roll-up can't reach.
    Gates of level ≥ L re-apply on the L row itself (Gender:Female means the ARTIST
    list holds only females, not males who share a matched track). ORDER by combined
    score, then matched-atom COUNT (browse mode: most matching atoms)."""
    gates_a, branches_a, at_terms, up_terms, lex_terms, lat_a = _matched_core(
        atom, tools, active, corpus, up_level=L.key)
    body, ctes = _matched_sql(atom, tools, gates_a, branches_a, at_terms, up_terms,
                              lex_terms, lat_a, corpus)
    link = _agg_link(atom, L, corpus)
    Lt = L.table.split()[0]
    Lrank = _LEVEL_RANK[L.key]
    gate_srcs = [s for t in tools for s in t.sources if s.is_gate]
    gates_L = [s.score_sql if s.table == Lt else _exists_gate(s, L, corpus)
               for s in gate_srcs if _LEVEL_RANK[_level(s)] >= Lrank]
    if L.key == "artist":
        # The compilation pseudo-artist contains a track for almost any query —
        # containment (and roll-ups generally) would surface it as a leading tile
        # that navigates nowhere useful.
        gates_L.append(f"{L.pk} != '{_VA_ID}'")
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
            if src.model and not _source_ready(src, active):
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
    rollup = f"{_ROLLUP_W} * GREATEST(COALESCE(r.s, 0), COALESCE(r.lex, 0))"
    score = (" + ".join(own_terms + [rollup])
             if (at_terms or own_terms) else "NULL")
    where = (" WHERE " + " AND ".join(gates_L)) if gates_L else ""
    # A matched atom enters the roll-up if it serves either channel; the FILTER
    # keeps the shrink denominator at the characteristic count alone, so a
    # title-only match doesn't dilute its container's sound-alike average.
    # NULL passes for browse mode, where roll-ups rank by count.
    # SUM/(n+3) = AVG shrunk toward 0 (Bayesian prior): one lucky strong atom no
    # longer outranks a genre/artist with many solid ones (Space Rock beat Jazz on
    # a single Pink Floyd sax track), and stray CLAP junk on an identity word damps
    # to noise level instead of minting flat 0.40 candidates. s_lex deliberately
    # skips the shrink (MAX) — containment is binary evidence, see docstring.
    # MATERIALIZED is load-bearing: matched is referenced once, so PG12+ would
    # inline it into rolled's nested loop and re-evaluate its per-row vector
    # laterals for every (matched x link) pair — measured 9.2s vs 38ms isolated.
    #
    # slim stays a slim projection (id/score/n) so L.surface's pile of correlated
    # subqueries only runs for the rows that survive. Its pool is limit+offset, not
    # limit: the dominance cut below divides by `SELECT MAX(score) FROM slim`, so the
    # LEADER must stay inside slim on every page — paging slim itself would rebase the
    # bar on each page's own top row and let junk back in. The two ORDER BYs are the
    # same key sequence (score, owned, matched-atom count, identity, pk) — the outer
    # one re-sorts what slim picked, so a divergence would scramble the very ties slim
    # ordered and make OFFSET pages overlap.
    owned_L = _OWNED_GUARD[L.key]
    tail = f"{L.default_order}, {L.pk}"
    order_slim = f"2 DESC NULLS LAST, ({owned_L}) DESC, r.n DESC NULLS LAST, {tail}"
    order_out = f"slim.score DESC NULLS LAST, is_owned DESC, slim.n DESC NULLS LAST, {tail}"
    sql = (f"WITH {ctes}matched AS MATERIALIZED ({body}), "
           f"rolled AS (SELECT lk.lid AS lid, "
           f"SUM(m.s_up) / (COUNT(*) FILTER (WHERE m.s_up > 0) + 3.0) AS s, "
           f"MAX(m.s_lex) AS lex, COUNT(*) AS n "
           f"FROM matched m {link} "
           f"WHERE m.s_up > 0 OR m.s_lex > 0 OR m.s_up IS NULL GROUP BY lk.lid), "
           f"slim AS (SELECT {L.pk} AS id, ({score}) AS score, r.n AS n "
           f"FROM (SELECT DISTINCT lid FROM ({cand}) c0) cd "
           f"JOIN {L.table} ON {L.pk} = cd.lid "
           f"LEFT JOIN rolled r ON r.lid = {L.pk} "
           f"{' '.join(own_lat)}{where} "
           f"ORDER BY {order_slim} "
           f"LIMIT {int(limit) + int(offset)}) "
           f"SELECT {L.pk} AS id, {L.name_col} AS name, slim.score{L.surface} "
           f"FROM slim JOIN {L.table} ON {L.pk} = slim.id "
           f"WHERE slim.score IS NULL "
           f"OR slim.score >= {_DOMINANCE_CUT} * (SELECT MAX(score) FROM slim) "
           f"ORDER BY {order_out} "
           f"LIMIT {int(limit)} OFFSET {int(offset)}")
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
    from hardware_profile import resolve as _hw
    # Profile veto. Without torch no model can ever load, so kicking would
    # spawn a doomed loader thread per query and bury the log in ImportErrors
    # — the model-backed sources stay skipped and the non-ML blocks serve.
    # The translator is vetoed one tier earlier: on lite a kick would drag
    # ~3GB into RAM against the tier policy, and the per-query predicate
    # keeps the sound scope English-only instead.
    if not _hw().ml_available:
        return
    if key == "translate" and not _hw().translation_available:
        return
    from routers.discovery import (_clap_loader, _enrichment_loader,
                                   _lyrics_loader, _translate_loader)
    factory = {"enrichment": _enrichment_loader, "clap": _clap_loader,
               "lyrics": _lyrics_loader, "translate": _translate_loader}.get(key)
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
    # CLAP's RoBERTa text encoder is savagely case-sensitive ('Romantic …'
    # vs 'romantic …' = cos 0.65, measured 2026-07-06): canonicalize to
    # lowercase and drop sentence punctuation (NLLB appends periods) so
    # typed caps and MT output land on the calibrated lowercase form.
    qn = q.strip().strip(".!?").strip().lower()
    return _to_vector_param(model_cache.get_model("clap", _load).text_to_embedding(qn))


def _encode_lyrics(q: str) -> str:
    import model_cache
    from search import _to_vector_param

    def _load():
        from lyrics_embeddings import LyricsEmbeddingGenerator
        g = LyricsEmbeddingGenerator()
        g.load_model()
        return g
    return _to_vector_param(model_cache.get_model("lyrics", _load).query_to_embedding(q))


def _clap_query(active: dict) -> str:
    """The effective CLAP text — only the sound tool carries a CLAP source
    since the search-scope split (the lexical text tool never encodes).
    Mirrors build()'s tool-activation predicate so the bind side never encodes
    for a tool the SQL side dropped."""
    v = active.get("sound")
    return str(v)[:255] if v not in (None, "", "any", []) else ""


def _clap_servable(active: dict) -> bool:
    """CLAP's text encoder is English-only; non-ASCII queries additionally
    need the local MT model (MADLAD infers the input language itself).
    Gating is PER QUERY, not per profile: an ASCII query is servable by
    CLAP alone, a non-ASCII one needs the translator warm too. One
    predicate covers every tier with no profile ifs — full keeps the
    translator warm so everything passes; standard lazy-loads it on the
    first non-ASCII query; lite vetoes the load so its sound scope is
    English-only (the router reports `limited`, not eternal warming).
    Must agree between SQL assembly and _bind_params — model_cache loads
    are monotonic, so a mid-build flip can only ADD an unused bind param,
    never leave a referenced one missing."""
    if not _model_ready("clap"):
        return False
    q = _clap_query(active)
    return (not q) or q.isascii() or _model_ready("translate")


def _source_ready(src, active: dict) -> bool:
    """Skip-or-serve for a model-backed source; kicks every cold piece
    (the model, and the translator when the CLAP query needs it) so the
    next request can serve."""
    if not _model_ready(src.model):
        _kick_model(src.model)
        return False
    if src.model == "clap" and not _clap_servable(active):
        _kick_model("translate")
        return False
    return True


def _to_english(q: str) -> str:
    """Non-ASCII query → English for CLAP encoding.

    ASCII queries BYPASS the translator on every profile — measured
    2026-07-21: int8 MADLAD is not identity-safe for English (beam dropped
    "with saxophone" from a plain English phrase, guards or not), and a
    deterministic bypass beats depending on model behavior. The residual —
    diacritic-less fr/de typed in ASCII hits CLAP untranslated — is the
    accepted trade from the sound-scope tier policy, now uniform across
    profiles. A cold translator is likewise only reachable by an ASCII
    query (per-query _clap_servable), which this same branch covers."""
    import model_cache
    if q.isascii() or not model_cache.is_loaded("translate"):
        return q
    from routers.discovery import _translate_loader
    return model_cache.get_model("translate", _translate_loader).to_english(q)


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
            # Lexical scope only since the search-scope split — no vector params.
            q = str(v)[:255]
            ql = (latinize(q) or q)[:255]
            p["q"] = q
            p["ql"], p["qlpfx"] = ql, ql + "%"
        elif tool.key == "bio":
            if _model_ready("enrichment"):
                p["qvec"] = _encode_bge(str(v)[:255])   # BGE-M3, multilingual
        elif tool.key == "sound":
            if _model_ready("enrichment"):
                p["qgdesc"] = _encode_bge(str(v)[:255])  # genre_desc side-channel
        elif tool.key in ("gender", "vocalist", "key", "mode"):
            p[tool.key] = v
        elif tool.key == "bpm":
            p["bpm_min"], p["bpm_max"] = v   # (min, max)
        elif tool.key == "genre":
            p["genre"] = list(v) if isinstance(v, (list, tuple)) else [v]
        elif tool.key == "moods":
            p["moods"] = list(v) if isinstance(v, (list, tuple)) else [v]
        elif tool.key == "instruments":
            import json

            from ensemble_instruments import threshold_for
            exp = tool.sources[0].expand
            labels = exp(v) if exp else (list(v) if isinstance(v, (list, tuple)) else [v])
            p["instruments"] = labels
            p["instruments_thr"] = json.dumps({lbl: threshold_for(lbl) for lbl in labels})
        elif tool.key == "energy":
            # energy_db buckets — terciles of the full-track amplitude
            # distribution (p33/p67 = -18.9/-13.9 measured 2026-07-03 after
            # the whole-track backfill; the old -25/-15 middle-30s bounds
            # left "low" with 7.6% of the library)
            buckets = {"low": (-100.0, -19.0), "mid": (-19.0, -14.0), "high": (-14.0, 0.0)}
            p["energy_lo"], p["energy_hi"] = buckets.get(v, (-100.0, 0.0))
        elif tool.key == "seed":
            from db_pool import db_query_one
            p["seed_tid"] = str(v)
            row = db_query_one(
                "SELECT vector::text AS v FROM embeddings WHERE track_id = %(t)s::uuid LIMIT 1",
                {"t": str(v)})
            # No embedding (un-analyzed phantom) → NULL vector: the seed branch
            # yields nothing and the search degrades to the other active tools.
            p["qseed"] = row["v"] if row else None
        elif tool.key == "artist":
            p["artist_ids"] = [str(x) for x in (v if isinstance(v, (list, tuple)) else [v])]
        elif tool.key == "lyrics":
            if _model_ready("lyrics"):
                p["qlyr"] = _encode_lyrics(str(v)[:255])
    # CLAP's text encoder is English-only — every sound query goes through the
    # local MADLAD translator (input language inferred by the model; English
    # passes as identity, no detection heuristics to get wrong). Until the
    # pair is warm _clap_servable skips the source. Only the sound tool
    # reaches CLAP, so the default names-scope path costs zero encodes.
    qc = _clap_query(active)
    if qc and _clap_servable(active):
        p["qclap"] = _encode_clap(_to_english(qc))
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
