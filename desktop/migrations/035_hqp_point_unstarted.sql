-- A benchmark point HQPlayer never got going (2026-10-03). While HQPlayer
-- builds a setting its control port answers nothing — minutes at times
-- (poly-sinc-long-lp at 44.1k × 256: 3 min 13 s on a laptop) — and a run
-- waits that out and measures the point. One HQPlayer is still building
-- past the run's build limit (10 min) — silent, or answering that it plays
-- with nothing out yet (Embedded) — is recorded as 'unstarted' once a
-- setting that started earlier in the run starts again: an answer like a
-- refusal, so a later run does not ask again (and does not take the machine
-- back into the swap a build can run it into). A silent HQPlayer ends the
-- run there; an answering one goes on. Mirrors the 001 baseline; a no-op on
-- a database 001 already created this way.

ALTER TYPE hqp_point_result ADD VALUE IF NOT EXISTS 'unstarted' BEFORE 'failed';
