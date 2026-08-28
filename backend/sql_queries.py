"""
Shared SQL building blocks for the canonical schema.

Used by: search.py, tools/definitions.py, routers/player.py, routers/chat.py,
         track_filter.py, and others.

All queries use the schema:
  tracks → track_artists → artists
  tracks → media_files → album_variants → albums
  albums → album_genres → genres   (genre is album-grain, not track)
  tracks → embeddings (via track_id)
  tracks → audio_features (via track_id)
"""

# ---------------------------------------------------------------------------
# Base SELECT for media-file-centric queries (search results, playback)
# Returns one row per media_file with track/artist/album info
# ---------------------------------------------------------------------------

MEDIA_FILE_SELECT = """\
    SELECT mf.id, t.title, a.name as artist, al.title as album,
           (SELECT g.name FROM album_genres ag JOIN genres g ON g.id = ag.genre_id
            WHERE ag.album_id = av.album_id ORDER BY ag.count DESC NULLS LAST LIMIT 1) as genre,
           mf.duration_seconds, mf.track_number, mf.disc_number,
           mf.sample_rate, mf.bit_depth, mf.is_lossless,
           mf.file_path"""

MEDIA_FILE_FROM = """\
    FROM media_files mf
    JOIN tracks t ON mf.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id
    JOIN album_variants av ON mf.album_variant_id = av.id
    JOIN albums al ON av.album_id = al.id"""

# ---------------------------------------------------------------------------
# Embedding similarity queries (track-centric, picks representative media_file)
# ---------------------------------------------------------------------------

EMBEDDING_SIMILARITY_SELECT = """\
    SELECT mf_rep.id, t.title, a.name as artist,
           mf_rep.album_title as album,
           (SELECT g.name FROM album_genres ag JOIN genres g ON g.id = ag.genre_id
            WHERE ag.album_id = mf_rep.album_id ORDER BY ag.count DESC NULLS LAST LIMIT 1) as genre,
           mf_rep.duration_seconds, mf_rep.track_number,
           mf_rep.sample_rate, mf_rep.bit_depth, mf_rep.is_lossless"""

EMBEDDING_SIMILARITY_FROM = """\
    FROM tracks t
    JOIN embeddings e ON e.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id
    JOIN LATERAL (
        SELECT mf.id, mf.duration_seconds, mf.track_number,
               mf.sample_rate, mf.bit_depth, mf.is_lossless,
               mf.file_path, al.title as album_title, al.id as album_id, al.release_year
        FROM media_files mf
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE mf.track_id = t.id
        ORDER BY mf.is_analysis_source DESC, mf.id
        LIMIT 1
    ) mf_rep ON true"""

# ---------------------------------------------------------------------------
# Search: trigram fuzzy search
# ---------------------------------------------------------------------------

SEARCH_TRACKS_FROM = """\
    FROM media_files mf
    JOIN tracks t ON mf.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id
    JOIN album_variants av ON mf.album_variant_id = av.id
    JOIN albums al ON av.album_id = al.id"""

# ---------------------------------------------------------------------------
# Play track: get file_path from media_file
# ---------------------------------------------------------------------------

PLAY_TRACK_SELECT = """\
    SELECT mf.file_path, t.title, a.name as artist, al.title as album"""

PLAY_TRACK_FROM = """\
    FROM media_files mf
    JOIN tracks t ON mf.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id
    JOIN album_variants av ON mf.album_variant_id = av.id
    JOIN albums al ON av.album_id = al.id"""

# ---------------------------------------------------------------------------
# Play album: find album, then get all tracks
# ---------------------------------------------------------------------------

ALBUM_MATCH_FROM = """\
    FROM albums al
    JOIN album_variants av ON av.album_id = al.id
    JOIN media_files mf ON mf.album_variant_id = av.id
    JOIN tracks t ON mf.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id"""

ALBUM_TRACKS_SELECT = """\
    SELECT mf.id, mf.file_path, t.title, mf.track_number, mf.disc_number,
           a.name as artist, al.title as album"""

ALBUM_TRACKS_FROM = """\
    FROM media_files mf
    JOIN tracks t ON mf.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id
    JOIN album_variants av ON mf.album_variant_id = av.id
    JOIN albums al ON av.album_id = al.id"""

# ---------------------------------------------------------------------------
# Play similar: embedding-based + file_path
# ---------------------------------------------------------------------------

SIMILAR_PLAY_SELECT = """\
    SELECT mf_rep.id, mf_rep.file_path, t.title, a.name as artist,
           mf_rep.album_title as album,
           1 - (e.vector <=> (SELECT vector FROM target)) as similarity"""

SIMILAR_PLAY_FROM = EMBEDDING_SIMILARITY_FROM

# ---------------------------------------------------------------------------
# Playback tracker: get track metadata
# ---------------------------------------------------------------------------

TRACKER_METADATA_SELECT = """\
    SELECT t.title, mf.duration_seconds as duration, al.title as album,
           a.name as artist"""

TRACKER_METADATA_FROM = """\
    FROM media_files mf
    JOIN tracks t ON mf.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id
    JOIN album_variants av ON mf.album_variant_id = av.id
    LEFT JOIN albums al ON av.album_id = al.id"""

# ---------------------------------------------------------------------------
# Playlist lookup: file_path → media_file info
# ---------------------------------------------------------------------------

PLAYLIST_TRACK_SELECT = """\
    SELECT mf.id, t.title, mf.track_number, a.name as artist"""

PLAYLIST_TRACK_FROM = """\
    FROM media_files mf
    JOIN tracks t ON mf.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id"""

# ---------------------------------------------------------------------------
# Chat validation: validate track IDs exist
# ---------------------------------------------------------------------------

VALIDATE_TRACKS_SELECT = """\
    SELECT mf.id, t.title, a.name as artist, al.title as album"""

VALIDATE_TRACKS_FROM = """\
    FROM media_files mf
    JOIN tracks t ON mf.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id
    JOIN album_variants av ON mf.album_variant_id = av.id
    JOIN albums al ON av.album_id = al.id"""

# ---------------------------------------------------------------------------
# Human engagement gate
# ---------------------------------------------------------------------------
# Anything that fans out per artist — one Last.fm call each — must be gated on
# a signal linear in human behaviour, and so must every coverage figure that
# reports on it. `track_artists` alone is not such a signal: the phantom
# tracklist mint credits every slot to its own artist, so on a dump node that
# column names a quarter-million stubs nobody asked about.
#
# Owned file OR completed, unskipped listen (the scrobble rule) — the listen
# arm is what makes streamed phantoms count in the catalog-less mode.
#
# Written as a set, not as correlated EXISTS on `artists`: both arms are driven
# from the small side (media_files, listening_history), so it costs one pass
# instead of one index probe per artist row — 63 ms against 5.3 s here.

ARTIST_ENGAGED_SET = """\
        SELECT ta.artist_id FROM track_artists ta
          JOIN media_files mf ON mf.track_id = ta.track_id
        UNION
        SELECT ta.artist_id FROM track_artists ta
          JOIN listening_history lh ON lh.track_id = ta.track_id
         WHERE lh.completed AND NOT lh.skipped"""

# WHERE-clause form, for candidate queries that select FROM artists a.
ARTIST_ENGAGED = f"""a.id IN (
{ARTIST_ENGAGED_SET}
    )"""

# ---------------------------------------------------------------------------
# Owned vs phantom (correlates against an `artists` row aliased `a`)
# ---------------------------------------------------------------------------
# The product's definition of a phantom artist, mirroring the `is_owned`
# expression the artist/discovery surfaces already ship: any owned file
# crediting them in ANY role. A featured artist on a track you own is owned —
# the UI shows them undimmed — so `role = 'primary'` is the wrong line here.

ARTIST_OWNED = """EXISTS (SELECT 1 FROM track_artists ta
                    JOIN media_files mf ON mf.track_id = ta.track_id
                   WHERE ta.artist_id = a.id)"""
