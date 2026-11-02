-- The settings rows the assistant's own queries read (assistant_queries.
-- active_hqp_endpoint). The assistant's login (desktop/db_init.ASSISTANT_ROLE)
-- reads this view, never the table, whose other rows hold keys and tokens.
-- A security barrier: a condition the reader adds sees only the rows it lets through.
CREATE OR REPLACE VIEW assistant_settings WITH (security_barrier = true) AS
    SELECT key, value FROM user_settings
     WHERE key IN ('output.type', 'hqplayer.host', 'hqplayer.port');
