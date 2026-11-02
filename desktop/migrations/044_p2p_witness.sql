-- Witness records (BIRTH-SYNC slice 1, 2026-11-02): the same DDL as the
-- p2p_witness block of 001_initial.sql, for nodes that ran the earlier
-- files. Idempotent: a no-op on the schema 001 just created. No backfill
-- from p2p_identities: its rows hold the current certificate, not the first
-- one seen, with no channel, and a stamp taken now would date them now.

-- The certificate's enums, created by 001 since 2026-08-17 and by the
-- registry's own ensure_schema, which runs after the migrations: a node
-- whose database predates both still gets them here.
DO $$ BEGIN
    CREATE TYPE p2p_cert_method AS ENUM ('pow', 'email');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    CREATE TYPE p2p_email_class AS ENUM ('major', 'other', 'disposable');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Witness records: what this node saw of other identities — the first
-- sighting of each certificate of each foreign identity, per channel
-- (desktop/p2p/witness.py). Witnessed age can count only what was recorded,
-- and the identity registry (p2p_identities) is no record: it is mutable,
-- evicted past its cap, rewritten by succession and filled only on the side
-- that was contacted.
-- A row keeps the certificate exactly as the authority signed it
-- (cert_payload + cert_sig; the other certificate columns are its fields,
-- for queries), how it came (via = the deliverer of a carried one), this
-- node's clock, and the notary's seal: this node's signature over the
-- statement, then a SELF-CONTAINED copy of the Worker stamp of the batch it
-- rode. No foreign key at all: signing_batches is emptied by an identity-rule
-- migration (canon/migrations._shed_all_seals), and a registry row is
-- evicted. Append-only, enforced below: a recorded sighting never changes —
-- only its seal fills, once — unlike the analysis tables, whose seal_guard
-- sheds a seal when content changes (there content may change; here it is a
-- past observation), and no row is ever deleted. Growth is bounded where
-- rows come in (per-channel daily ceilings), never by deletion. Life data,
-- local only: never in the sync protocol, a share export or the seed.
DO $$ BEGIN
    CREATE TYPE p2p_witness_channel AS ENUM ('introduced', 'handshake', 'probe', 'carried');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS p2p_witness (
    id                  BIGSERIAL PRIMARY KEY,
    witness_v           SMALLINT NOT NULL DEFAULT 1,   -- the statement's payload version (witness.py)
    subject             TEXT NOT NULL,                 -- hex Ed25519 of the identity seen
    cert_payload        TEXT NOT NULL,                 -- 'sautium-birth:v4:…', exactly as signed
    cert_sig            TEXT NOT NULL,
    issuer              TEXT NOT NULL,
    cert_v              SMALLINT NOT NULL,
    issued_at           TIMESTAMPTZ NOT NULL,
    method              p2p_cert_method NOT NULL,
    difficulty          BIGINT NOT NULL,
    params_version      SMALLINT NOT NULL,
    email_token         TEXT,
    email_class         p2p_email_class,
    email_domain_token  TEXT,
    predecessor         TEXT,
    channel             p2p_witness_channel NOT NULL,
    via                 TEXT,                          -- the deliverer of a carried certificate
    seen_at             TIMESTAMPTZ NOT NULL DEFAULT date_trunc('second', now()),
    author_pubkey       CHAR(64),                      -- the seal: the witness's key and signature …
    signature           CHAR(128),
    batch_root          CHAR(64),                      -- … then the batch and a copy of its stamp
    merkle_proof        JSONB,
    worker_date         TIMESTAMPTZ,
    worker_ip_hash      UUID,
    worker_sig          CHAR(128),
    worker_authority    CHAR(64),
    timestamp_version   SMALLINT,
    UNIQUE (subject, cert_sig, channel),
    -- One spelling of each key and signature, the authority's: hex parsers
    -- take upper case and spaced pairs, and each spelling would be a row.
    CONSTRAINT p2p_witness_hex CHECK (subject ~ '^[0-9a-f]{64}$' AND cert_sig ~ '^[0-9a-f]{128}$'
                                      AND (via IS NULL OR via ~ '^[0-9a-f]{64}$')),
    CONSTRAINT p2p_witness_via_carried CHECK ((channel = 'carried') = (via IS NOT NULL)),
    CONSTRAINT p2p_witness_signed_whole CHECK ((author_pubkey IS NULL) = (signature IS NULL)),
    CONSTRAINT p2p_witness_stamped_signed CHECK (batch_root IS NULL OR signature IS NOT NULL),
    CONSTRAINT p2p_witness_stamp_whole CHECK (
        num_nulls(batch_root, merkle_proof, worker_date, worker_ip_hash,
                  worker_sig, worker_authority, timestamp_version) IN (0, 7))
);
CREATE INDEX IF NOT EXISTS idx_p2p_witness_unsigned
    ON p2p_witness (id) WHERE signature IS NULL;
-- Signed, awaiting a stamp: what a stamp picks up (by author), and the oldest
-- of them, which the notary's backstop is armed from on every pass.
CREATE INDEX IF NOT EXISTS idx_p2p_witness_unstamped
    ON p2p_witness (id) INCLUDE (author_pubkey, signature)
    WHERE signature IS NOT NULL AND batch_root IS NULL;
CREATE INDEX IF NOT EXISTS idx_p2p_witness_waiting
    ON p2p_witness (seen_at) WHERE signature IS NOT NULL AND batch_root IS NULL;
CREATE INDEX IF NOT EXISTS idx_p2p_witness_channel_seen ON p2p_witness (channel, seen_at);

-- The notary signs a row the moment it lands: one wake per statement that
-- inserted anything, heard by the backend's listener on both runtimes (the
-- launcher records from its own process, not its backend's).
CREATE OR REPLACE FUNCTION notify_witness_recorded() RETURNS TRIGGER AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM added) THEN
        PERFORM pg_notify('sautium_witness', '');
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql;

DO $$ BEGIN
    CREATE TRIGGER trg_p2p_witness_notify
    AFTER INSERT ON p2p_witness
    REFERENCING NEW TABLE AS added
    FOR EACH STATEMENT EXECUTE FUNCTION notify_witness_recorded();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Evidence that can be changed or deleted is not evidence, and a loud error
-- is the right failure. The seal columns are the only ones that move: the
-- signature once, then the stamp once; a stamped row is final.
CREATE OR REPLACE FUNCTION p2p_witness_guard_update() RETURNS TRIGGER AS $$
DECLARE
    seal CONSTANT text[] := ARRAY['author_pubkey', 'signature', 'batch_root', 'merkle_proof',
                                  'worker_date', 'worker_ip_hash', 'worker_sig',
                                  'worker_authority', 'timestamp_version'];
BEGIN
    IF to_jsonb(NEW) - seal IS DISTINCT FROM to_jsonb(OLD) - seal
       OR (OLD.signature IS NOT NULL
           AND ROW(NEW.author_pubkey, NEW.signature) IS DISTINCT FROM ROW(OLD.author_pubkey, OLD.signature))
       OR (OLD.batch_root IS NOT NULL AND to_jsonb(NEW) IS DISTINCT FROM to_jsonb(OLD)) THEN
        RAISE EXCEPTION 'p2p_witness is evidence: a recorded sighting never changes — only its seal fills, once'
            USING HINT = 'docs/design/P2P-SYNC-INTEGRITY.md § "Witness records"';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION p2p_witness_guard_delete() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'p2p_witness is evidence: a witness record is never deleted (%)', TG_OP
        USING HINT = 'docs/design/P2P-SYNC-INTEGRITY.md § "Witness records"';
END $$ LANGUAGE plpgsql;

DO $$ BEGIN
    CREATE TRIGGER trg_p2p_witness_guard_update
    BEFORE UPDATE ON p2p_witness
    FOR EACH ROW EXECUTE FUNCTION p2p_witness_guard_update();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_p2p_witness_guard_delete
    BEFORE DELETE ON p2p_witness
    FOR EACH STATEMENT EXECUTE FUNCTION p2p_witness_guard_delete();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TRIGGER trg_p2p_witness_guard_truncate
    BEFORE TRUNCATE ON p2p_witness
    FOR EACH STATEMENT EXECUTE FUNCTION p2p_witness_guard_delete();
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
