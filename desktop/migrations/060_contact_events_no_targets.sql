-- 060 — the contact log keeps no name a request carried (2026-11-14). It
-- kept the first eight names or MBIDs of each slice request, and a search's
-- text, with the requester's key and address for 30 days: a record of which
-- artists a node asked for. The column goes, and a request about artists — a
-- slice of either family, a catalogue search — leaves no row at all, only its
-- cost sample (desktop/p2p/contact_log.py); the rows it left before are
-- deleted once (backend/db_migrate.py `slice_records_cut_v1`). The same DDL
-- as the p2p_contact_events block of 001_initial.sql, for nodes that ran the
-- earlier files. Idempotent: a no-op on the schema 001 just created.

ALTER TABLE p2p_contact_events DROP COLUMN IF EXISTS targets;
