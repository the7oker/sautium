-- The zero-shot values derived, not analysed (ANALYSIS-3 body 1, 2026-11-10):
-- CLAP's moods, vocal score and danceability become scores the database
-- derives from the segment embeddings a node holds, kept by two statement
-- triggers on embedding_segments, against label embeddings that ship with
-- the release (backend/zero_shot_labels/labels.json — loaded, and every held
-- bundle scored, by db_migrate's zero_shot_labels_v1 step). The same DDL as
-- the "Zero-shot scores" block of 001_initial.sql and its
-- embedding_models.logit_scale, where the why of each object is; for nodes
-- that ran the earlier files. Idempotent: a no-op on the schema 001 just
-- created.

ALTER TABLE embedding_models ADD COLUMN IF NOT EXISTS logit_scale DOUBLE PRECISION;

DO $$ BEGIN
    CREATE TYPE zero_shot_set AS ENUM ('moods', 'vocal', 'dance');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS zero_shot_labels (
    id SMALLSERIAL PRIMARY KEY,
    model_id UUID NOT NULL REFERENCES embedding_models(id) ON DELETE CASCADE ON UPDATE CASCADE,
    label_set zero_shot_set NOT NULL,
    key VARCHAR(64) NOT NULL,
    prompt TEXT NOT NULL,
    vector vector(512) NOT NULL,
    CONSTRAINT uq_zero_shot_labels_key UNIQUE (model_id, key)
);
ALTER TABLE zero_shot_labels ALTER COLUMN vector SET STORAGE PLAIN;

CREATE TABLE IF NOT EXISTS zero_shot_scores (
    embedding_id INTEGER NOT NULL REFERENCES embeddings(id) ON DELETE CASCADE,
    label_id SMALLINT NOT NULL REFERENCES zero_shot_labels(id) ON DELETE CASCADE,
    score DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (embedding_id, label_id),
    CONSTRAINT chk_zss_score CHECK (score >= 0 AND score <= 1)
);
CREATE INDEX IF NOT EXISTS idx_zero_shot_scores_label_score
    ON zero_shot_scores (label_id, score);

CREATE OR REPLACE FUNCTION zero_shot_derive(ids INTEGER[])
RETURNS TABLE (embedding_id INTEGER, label_id SMALLINT, score DOUBLE PRECISION)
LANGUAGE sql STABLE AS $$
    SELECT p.embedding_id, p.label_id, avg(p.p)
    FROM (
        SELECT x.embedding_id, x.label_id,
               x.w / sum(x.w) OVER (PARTITION BY x.segment_id, x.label_set) AS p
        FROM (
            SELECT s.segment_id, s.embedding_id, l.id AS label_id, l.label_set,
                   exp(s.logit_scale * -(s.vector <#> l.vector)) AS w
            FROM (SELECT es.id AS segment_id, es.embedding_id, e.model_id, m.logit_scale,
                         subvector(es.vector, 1, 512) AS vector
                  FROM embedding_segments es
                  JOIN embeddings e ON e.id = es.embedding_id
                  JOIN embedding_models m ON m.id = e.model_id
                  WHERE es.embedding_id = ANY(ids)
                  OFFSET 0) s
            JOIN zero_shot_labels l ON l.model_id = s.model_id
            OFFSET 0
        ) x
    ) p
    GROUP BY p.embedding_id, p.label_id
$$;

CREATE OR REPLACE FUNCTION zero_shot_scores_refresh() RETURNS trigger AS $$
DECLARE
    touched INTEGER[] := ARRAY(SELECT DISTINCT embedding_id FROM changed);
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM zero_shot_scores s
         WHERE s.embedding_id = ANY(touched)
           AND NOT EXISTS (SELECT 1 FROM embedding_segments es
                            WHERE es.embedding_id = s.embedding_id);
    END IF;
    INSERT INTO zero_shot_scores (embedding_id, label_id, score)
    SELECT d.embedding_id, d.label_id, d.score FROM zero_shot_derive(touched) d
    ON CONFLICT (embedding_id, label_id) DO UPDATE SET score = EXCLUDED.score;
    RETURN NULL;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_embedding_segments_scores_ins ON embedding_segments;
CREATE TRIGGER trg_embedding_segments_scores_ins
AFTER INSERT ON embedding_segments
REFERENCING NEW TABLE AS changed
FOR EACH STATEMENT EXECUTE FUNCTION zero_shot_scores_refresh();

DROP TRIGGER IF EXISTS trg_embedding_segments_scores_del ON embedding_segments;
CREATE TRIGGER trg_embedding_segments_scores_del
AFTER DELETE ON embedding_segments
REFERENCING OLD TABLE AS changed
FOR EACH STATEMENT EXECUTE FUNCTION zero_shot_scores_refresh();

CREATE OR REPLACE VIEW track_zero_shot AS
SELECT e.track_id, e.model_id,
       (SELECT jsonb_object_agg(l.key, s.score)
          FROM zero_shot_scores s JOIN zero_shot_labels l ON l.id = s.label_id
         WHERE s.embedding_id = e.id AND l.label_set = 'moods') AS moods,
       (SELECT s.score
          FROM zero_shot_scores s JOIN zero_shot_labels l ON l.id = s.label_id
         WHERE s.embedding_id = e.id AND l.model_id = e.model_id AND l.key = 'vocal') AS vocal_score,
       (SELECT s.score
          FROM zero_shot_scores s JOIN zero_shot_labels l ON l.id = s.label_id
         WHERE s.embedding_id = e.id AND l.model_id = e.model_id AND l.key = 'danceable') AS danceability
FROM embeddings e
WHERE EXISTS (SELECT 1 FROM zero_shot_scores s WHERE s.embedding_id = e.id);
