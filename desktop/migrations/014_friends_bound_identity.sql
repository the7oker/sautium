-- Which of OUR keys a friend binds our invite code to (see the friends
-- block in 001). A key rotation changes the invite code, so a re-introduction
-- by token makes the peer a SECOND friendship instead of migrating the first
-- — only the signed rotation notice can migrate it, and it is retried per
-- friend until accepted. NULL on existing rows = unknown: the notice chain is
-- walked, and every notice a friend does not know us by is a no-op.
ALTER TABLE friends ADD COLUMN IF NOT EXISTS bound_identity VARCHAR(128);
