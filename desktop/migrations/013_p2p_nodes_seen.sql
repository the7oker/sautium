-- Every reachable peer this node has met, by node key — the ledger the
-- network-size estimate is taken against (desktop/p2p/network_size.py:
-- capture-recapture between a run's DHT sample and the nodes met before).
-- Host as dialed (a compressed IP or a lowercased name), the port beside it;
-- rows unseen for the sighting window are dropped by the estimator.
CREATE TABLE IF NOT EXISTS p2p_nodes_seen (
    pubkey         TEXT PRIMARY KEY,              -- hex Ed25519: the TLS-verified /health node_id
    host           TEXT NOT NULL,
    port           INTEGER NOT NULL,
    first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    sightings      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_p2p_nodes_seen_addr ON p2p_nodes_seen (host, port);
