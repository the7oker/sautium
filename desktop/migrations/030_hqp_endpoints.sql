-- An HQPlayer's library is identified by an hqp_endpoints row, not by its
-- address (2026-09-28). A friend's streamer, or the Pi after a DHCP lease,
-- comes back at another host:port and is still the same library: the row
-- keeps the owner's name for it, what HQPlayer reports about itself
-- (<GetInfo/>), the <LibraryGetHash/> the last complete sync saw (the
-- same library at a new address matches on it) and where that HQPlayer
-- mounts THIS node's library, so the files it can already reach by path
-- are never imported as copies. album_variants carries the endpoint id;
-- the per-endpoint hash setting moves into the row. Mirrors the 001
-- baseline; a no-op on a database 001 already created this way.

CREATE TABLE IF NOT EXISTS hqp_endpoints (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,                     -- the owner's label; HQPlayer's own name at first sight
    host TEXT,                              -- the address last seen answering; NULL once another HQPlayer took it
    port INTEGER,
    product TEXT,                           -- <GetInfo product="…"/>
    hqp_name TEXT,                          -- <GetInfo name="…"/>
    library_hash TEXT,                      -- <LibraryGetHash/> the last COMPLETE sync saw; NULL = never imported
    library_root TEXT,                      -- where this HQPlayer mounts this node's library (path mode); NULL = same filesystem or none
    library_root_local TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_synced_at TIMESTAMPTZ,
    CONSTRAINT chk_hqp_endpoints_address CHECK ((host IS NULL) = (port IS NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_hqp_endpoints_address ON hqp_endpoints(host, port) WHERE host IS NOT NULL;

ALTER TABLE album_variants
    ADD COLUMN IF NOT EXISTS hqp_endpoint_id INTEGER REFERENCES hqp_endpoints(id) ON DELETE CASCADE;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'album_variants' AND column_name = 'hqp_endpoint_host') THEN
        -- one endpoint per address the variants or the hash settings know
        INSERT INTO hqp_endpoints (name, host, port, library_hash)
        SELECT a.host, a.host, a.port,
               (SELECT us.value #>> '{}' FROM user_settings us
                 WHERE us.key = 'hqp_library.hash:' || a.host || ':' || a.port)
        FROM (SELECT DISTINCT hqp_endpoint_host AS host, hqp_endpoint_port AS port
                FROM album_variants WHERE hqp_endpoint_host IS NOT NULL
              UNION
              SELECT (regexp_match(key, '^hqp_library\.hash:(.*):(\d+)$'))[1],
                     (regexp_match(key, '^hqp_library\.hash:(.*):(\d+)$'))[2]::int
                FROM user_settings WHERE key ~ '^hqp_library\.hash:.*:\d+$') a
        WHERE NOT EXISTS (SELECT 1 FROM hqp_endpoints e WHERE e.host = a.host AND e.port = a.port);
        UPDATE album_variants av SET hqp_endpoint_id = e.id
          FROM hqp_endpoints e
         WHERE av.hqp_endpoint_host = e.host AND av.hqp_endpoint_port = e.port
           AND av.hqp_endpoint_id IS NULL;
        DELETE FROM user_settings WHERE key ~ '^hqp_library\.hash:.*:\d+$';
        ALTER TABLE album_variants DROP CONSTRAINT IF EXISTS album_variants_dir_album_endpoint_key;
        ALTER TABLE album_variants DROP CONSTRAINT IF EXISTS chk_album_variants_location;
        ALTER TABLE album_variants DROP COLUMN hqp_endpoint_host, DROP COLUMN hqp_endpoint_port;
    END IF;
END $$;

ALTER TABLE album_variants DROP CONSTRAINT IF EXISTS album_variants_dir_album_endpoint_key;
ALTER TABLE album_variants ADD CONSTRAINT album_variants_dir_album_endpoint_key
    UNIQUE NULLS NOT DISTINCT (directory_path, album_id, hqp_endpoint_id);
ALTER TABLE album_variants DROP CONSTRAINT IF EXISTS chk_album_variants_location;
ALTER TABLE album_variants ADD CONSTRAINT chk_album_variants_location CHECK (
    (location = 'local' AND hqp_endpoint_id IS NULL)
    OR (location = 'hqplayer' AND hqp_endpoint_id IS NOT NULL));
CREATE INDEX IF NOT EXISTS idx_album_variants_hqp_endpoint
    ON album_variants(hqp_endpoint_id) WHERE hqp_endpoint_id IS NOT NULL;
