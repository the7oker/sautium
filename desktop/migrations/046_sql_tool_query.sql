-- The agents' SQL tool runs its query here (mcp/postgres_server.py). The
-- caller's text is opened as a cursor (OPEN ... FOR EXECUTE), which
-- PostgreSQL does only for ONE statement that returns rows, and read one row
-- at a time (a FOR loop prefetches them by tens); the rows that fit come
-- back -- the array's brackets and commas counted -- and, when more matched,
-- one last row naming what cut them: 'rows' (the cap) or 'size' (the
-- budget). A row whose stored size is past eight times what is left of the
-- budget is cut before its JSON is made: no text renders that much smaller
-- than its storage. The schema's, run with the caller's rights: the tool's
-- login (desktop/db_init.ASSISTANT_ROLE) creates nothing, not even in pg_temp.
CREATE OR REPLACE FUNCTION sautium_query(q text, cap integer, budget integer)
RETURNS TABLE (answer_row text, cut text)
LANGUAGE plpgsql AS $$
DECLARE
    curs refcursor;
    r record;
    n integer := 0;
    used bigint := 2;
BEGIN
    OPEN curs FOR EXECUTE q;
    LOOP
        FETCH curs INTO r;
        EXIT WHEN NOT FOUND;
        IF n = cap THEN
            answer_row := NULL; cut := 'rows'; RETURN NEXT; RETURN;
        END IF;
        IF pg_column_size(r) > 8 * (budget - used) + 1024 THEN
            answer_row := NULL; cut := 'size'; RETURN NEXT; RETURN;
        END IF;
        answer_row := row_to_json(r)::text;
        used := used + length(answer_row) + CASE WHEN n > 0 THEN 1 ELSE 0 END;
        IF used > budget THEN
            answer_row := NULL; cut := 'size'; RETURN NEXT; RETURN;
        END IF;
        n := n + 1;
        cut := NULL;
        RETURN NEXT;
    END LOOP;
END
$$;
