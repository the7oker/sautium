-- The settings rows the assistant's own queries read (assistant_queries.
-- active_hqp_endpoint). The assistant's login (desktop/db_init.ASSISTANT_ROLE)
-- reads this view, never the table, whose other rows hold keys and tokens.
CREATE OR REPLACE VIEW assistant_settings AS
    SELECT key, value FROM user_settings
     WHERE key IN ('output.type', 'hqplayer.host', 'hqplayer.port');
