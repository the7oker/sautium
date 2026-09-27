"""Cover Art Archive conventions — one literal for every path that mints a
phantom's cover (discography, mb_discovery, the seed / share exporters).
Kept apart from those modules on purpose: the exporters run as a CLI on the
launcher's interpreter, and importing the URL through discography dragged
in mb_backend and a database pool for one string."""

CAA_FRONT_URL = "https://coverartarchive.org/release-group/{rg}/front-500"
_CAA_PREFIX, _CAA_SUFFIX = CAA_FRONT_URL.split("{rg}")


def fill_held_album_covers(artist_id: str | None = None) -> int:
    """Albums held only in an HQPlayer's library have no bytes here to
    extract art from: give the ones the canon bound to a release group the
    Cover Art Archive front, the way a phantom album carries it — after a
    library sync, and again whenever an artist's discography is reconciled
    (the canon binds an album to its release group after the import, on
    its own clock). Never touches an album with a file cover or an existing
    url. Returns the number of albums given a cover."""
    from db_pool import db_query
    rows = db_query(f"""
        UPDATE albums al
        SET cover_url = %(prefix)s || al.musicbrainz_id::text || %(suffix)s,
            updated_at = now()
        WHERE al.musicbrainz_id IS NOT NULL AND al.cover_url IS NULL
          AND EXISTS (SELECT 1 FROM album_variants av
                      WHERE av.album_id = al.id AND av.location = 'hqplayer')
          AND NOT EXISTS (SELECT 1 FROM media_files mf
                          JOIN album_variants av ON av.id = mf.album_variant_id
                          WHERE av.album_id = al.id AND mf.cover_id IS NOT NULL)
          {"AND EXISTS (SELECT 1 FROM album_artists aa WHERE aa.album_id = al.id AND aa.artist_id = %(artist)s::uuid)" if artist_id else ""}
        RETURNING al.id
    """, {"prefix": _CAA_PREFIX, "suffix": _CAA_SUFFIX, "artist": artist_id})
    return len(rows)
