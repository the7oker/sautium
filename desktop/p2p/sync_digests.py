"""Set-digest SQL for the multi-row enrichment categories.

Deliberately dependency-free. Both server implementations need these
expressions and the container loads this file by path — anything imported here
would have to resolve inside the image too, which is how the first attempt
failed. There is nothing to import: these are two strings, and the reason they
live alone in a file is that a peer and a client computing them differently
would make every artist look divergent to everyone, forever.

Inventory answers "what can you give me". For one-row-per-entity data (a bio,
a stats line) presence is the whole answer. Tags and similar artists are SETS,
and presence is a terrible proxy: a node holding one tag for an artist looked
identical to a node holding fifteen, so it never asked for the other fourteen.

Over names and sources, not weights. Weights drift at the source, so hashing
them would make every artist look divergent forever and turn a gap-filling
sync into a full re-pull every run. Weight updates still ride along whenever a
pull happens for a real reason.
"""

TAG_SET_DIGEST = "md5(string_agg(DISTINCT lower(t.name) || ':' || at2.source, ','))"

SIMILAR_SET_DIGEST = (
    "md5(string_agg(DISTINCT sa.similar_artist_id::text || ':' || sa.source, ','))")
