"""
Shared SQL building blocks for the canonical schema.

Used by: search.py, tools/definitions.py, routers/player.py, routers/chat.py,
         track_filter.py, and others.

All queries use the schema:
  tracks → track_artists → artists
  tracks → media_files → album_variants → albums
  tracks → track_genres → genres
  tracks → embeddings (via track_id)
  tracks → audio_features (via track_id)
"""

# ---------------------------------------------------------------------------
# Base SELECT for media-file-centric queries (search results, playback)
# Returns one row per media_file with track/artist/album info
# ---------------------------------------------------------------------------

MEDIA_FILE_SELECT = """\
    SELECT mf.id, t.title, a.name as artist, al.title as album,
           g.name as genre,
           mf.duration_seconds, mf.track_number, mf.disc_number,
           mf.sample_rate, mf.bit_depth, mf.is_lossless,
           mf.file_path"""

MEDIA_FILE_FROM = """\
    FROM media_files mf
    JOIN tracks t ON mf.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id
    JOIN album_variants av ON mf.album_variant_id = av.id
    JOIN albums al ON av.album_id = al.id
    LEFT JOIN track_genres tg ON t.id = tg.track_id
    LEFT JOIN genres g ON tg.genre_id = g.id"""

# ---------------------------------------------------------------------------
# Embedding similarity queries (track-centric, picks representative media_file)
# ---------------------------------------------------------------------------

EMBEDDING_SIMILARITY_SELECT = """\
    SELECT mf_rep.id, t.title, a.name as artist,
           mf_rep.album_title as album, g.name as genre,
           mf_rep.duration_seconds, mf_rep.track_number,
           mf_rep.sample_rate, mf_rep.bit_depth, mf_rep.is_lossless"""

EMBEDDING_SIMILARITY_FROM = """\
    FROM tracks t
    JOIN embeddings e ON e.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id
    LEFT JOIN track_genres tg ON t.id = tg.track_id
    LEFT JOIN genres g ON tg.genre_id = g.id
    JOIN LATERAL (
        SELECT mf.id, mf.duration_seconds, mf.track_number,
               mf.sample_rate, mf.bit_depth, mf.is_lossless,
               mf.file_path, al.title as album_title, al.release_year
        FROM media_files mf
        JOIN album_variants av ON mf.album_variant_id = av.id
        JOIN albums al ON av.album_id = al.id
        WHERE mf.track_id = t.id
        ORDER BY mf.is_analysis_source DESC, mf.id
        LIMIT 1
    ) mf_rep ON true"""

# ---------------------------------------------------------------------------
# Simple track info query (for track info / validation)
# ---------------------------------------------------------------------------

TRACK_INFO_SELECT = """\
    SELECT mf.id, t.title, a.name as artist, al.title as album,
           al.release_year, g.name as genre,
           mf.track_number, mf.disc_number,
           mf.duration_seconds, mf.sample_rate, mf.bit_depth,
           mf.is_lossless, mf.file_path"""

TRACK_INFO_FROM = MEDIA_FILE_FROM

# ---------------------------------------------------------------------------
# Search: trigram fuzzy search
# ---------------------------------------------------------------------------

SEARCH_TRACKS_FROM = """\
    FROM media_files mf
    JOIN tracks t ON mf.track_id = t.id
    JOIN track_artists ta ON t.id = ta.track_id AND ta.role = 'primary'
    JOIN artists a ON ta.artist_id = a.id
    JOIN album_variants av ON mf.album_variant_id = av.id
    JOIN albums al ON av.album_id = al.id
    LEFT JOIN track_genres tg ON t.id = tg.track_id
    LEFT JOIN genres g ON tg.genre_id = g.id"""

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
