-- libtorrent DHT routing-table snapshot (session_params dht_state): saved
-- at every key re-announce cycle and on shutdown, loaded when the session
-- is created, so the node rejoins the DHT from its last known neighbours
-- instead of the public bootstrap routers — seconds instead of a 30 s
-- bootstrap, and a way in where UDP to those routers is throttled. One row
-- per node; both runtimes keep theirs in their own database
-- (desktop/p2p/dht_state.py).
CREATE TABLE IF NOT EXISTS p2p_dht_state (
    id        SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    state     BYTEA NOT NULL,
    saved_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
