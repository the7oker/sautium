-- 079 — LIBRARY-FOLDERS-1 (2026-11-26): the same DDL as 001_initial.sql's
-- block, for nodes that ran the earlier files. Idempotent: a no-op on the
-- schema 001 just created. The rows of the table it replaces,
-- library_roots (one chosen folder, the newest), are read and the table
-- dropped by db_migrate's data step library_folders_v1.
--
-- The library's folders (LIBRARY-FOLDERS-1, 2026-11-26): every folder the
-- person has given the library — picked under the roots in the Web UI, added
-- in the launcher's window, or named in the node's .env — several at once.
-- `path` is the folder as media_files.file_path spells it (forward slashes, no
-- trailing separator): the prefix of every file it holds, and its identity.
-- Under the folder disks are mounted in, the path carries the filesystem's
-- UUID (the automount's and the setup's own mount point), so a disk keeps its
-- folder whoever mounts it and wherever it is plugged; elsewhere it is the
-- path the person gave. No folder lies inside another (the one writer,
-- library_root.add, refuses the inner and merges the outer under a lock of
-- this table). Whether a folder answers now is the process's look, never
-- stored. `label`: the disk's label as it was last read (a stranger's text,
-- for the page alone) — the automount removes its own copy with the disk, and
-- the row of a disk that is out still names it.
CREATE TABLE IF NOT EXISTS library_folders (
    id SERIAL PRIMARY KEY,
    path TEXT NOT NULL,
    label TEXT,
    added_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT library_folders_path UNIQUE (path)
);
