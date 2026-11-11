"""A start that finds no identity (desktop/node_identity.ensure_identity — the
launcher settles its identity there, through IdentitySettling, before P2P
runs): the key on disk is the identity, and the database is not read; with
none, a database that says nothing of an identity gets a new one; one that
holds a seal of this node's own — its analysis, its canon rows, its witness
statements — or the key its last P2P run held gets none, and only the owner's
choice (generate_identity) makes one; a database that cannot be read decides
nothing. A new identity is never written over a key, and the info of one
whose key is gone is moved aside. The settling runs one operation at a time:
no check reads the folder a mint is writing, and no two completions start
P2P. The analysis arms are the check the records' cutover fails closed on
(SEALED_FIRST_HAND_SQL). On a real PostgreSQL built from the migrations;
skipped without a cluster."""

import json
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from desktop import node_identity, sync_client  # noqa: E402

MODEL = sync_client.model_uuid("clap")
SEAL = ("ab" * 32, "5e" * 64)                      # an author key and a signature, as stored


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """The identity folder of a config dir of its own."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    return tmp_path / "Sautium" / "node_identity"


@pytest.fixture
def conn(scratch_dsn):
    c = psycopg2.connect(scratch_dsn)
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("TRUNCATE tracks, albums, embedding_models CASCADE")
        cur.execute("DELETE FROM user_settings WHERE key IN ('p2p.bound_identity', "
                    "'identity.signing_key')")
        cur.execute("INSERT INTO embedding_models (id, name, dimension) VALUES (%s, 'clap', 512)",
                    (MODEL,))
    yield c
    c.close()


def _track(cur) -> str:
    tid = str(uuid.uuid4())
    cur.execute("INSERT INTO tracks (id, title) VALUES (%s, 't')", (tid,))
    return tid


def _source(cur, track, imported=False) -> int:
    cur.execute("""INSERT INTO analysis_sources (track_id, chromaprint, duration_seconds, imported)
                   VALUES (%s, 'AQAAidentity', 240, %s) RETURNING id""", (track, imported))
    return cur.fetchone()[0]


def _record(cur, kind, source, seal=(None, None)) -> None:
    """A record of `kind` under `source` (None: under no source)."""
    track = _track(cur)
    if kind == "segments":
        cur.execute("""INSERT INTO embeddings (track_id, model_id, vector, analysis_source_id)
                       VALUES (%s, %s, %s::vector, %s) RETURNING id""",
                    (track, MODEL, "[" + ",".join(["0.04"] * 512) + "]", source))
        cur.execute("""INSERT INTO embedding_segments (embedding_id, segment_index, vector,
                                                       author_pubkey, signature)
                       VALUES (%s, 0, %s::vector, %s, %s)""",
                    (cur.fetchone()[0], "[" + ",".join(["0.04"] * 512) + "]", *seal))
    elif kind == "instruments":
        cur.execute("""INSERT INTO track_instruments (track_id, instruments, analysis_source_id,
                                                      analysis_version, author_pubkey, signature)
                       VALUES (%s, '{}', %s, 2, %s, %s)""", (track, source, *seal))
    else:
        cur.execute("""INSERT INTO audio_features (track_id, bpm, analysis_source_id,
                                                   author_pubkey, signature)
                       VALUES (%s, 99.0, %s, %s, %s)""", (track, source, *seal))


def _canon(cur, kind, imported=False) -> None:
    """A sealed canon row of `kind`: an album, a tracklist row, a recording binding."""
    album, track = str(uuid.uuid4()), _track(cur)
    seal = SEAL if kind == "albums" else (None, None)
    cur.execute("""INSERT INTO albums (id, title, author_pubkey, signature, imported)
                   VALUES (%s, 'a', %s, %s, %s)""", (album, *seal, imported))
    if kind == "album_tracks":
        cur.execute("""INSERT INTO album_tracks (album_id, track_id, position, author_pubkey,
                                                 signature, imported)
                       VALUES (%s, %s, 1, %s, %s, %s)""", (album, track, *SEAL, imported))
    elif kind == "track_mbids":
        cur.execute("""INSERT INTO track_mbids (recording_mbid, track_id, author_pubkey, signature,
                                                imported)
                       VALUES (%s, %s, %s, %s, %s)""", (str(uuid.uuid4()), track, *SEAL, imported))


UNREACHABLE = "postgresql://nobody:none@127.0.0.1:1/none"


def _key_on_disk(folder) -> str:
    return json.loads((folder / "node_info.json").read_text())["public_key_hex"]


def test_the_identity_on_disk_is_the_one_a_start_takes(conn, scratch_dsn, folder):
    with conn.cursor() as cur:
        _record(cur, "features", _source(cur, _track(cur)), SEAL)
    node = node_identity.generate_identity()
    assert node_identity.ensure_identity(scratch_dsn) == "present"
    assert json.loads((folder / "node_info.json").read_text())["node_id"] == node
    assert node_identity.ensure_identity(UNREACHABLE) == "present"   # the database is not read


def test_a_key_without_its_info_is_the_identity_and_gets_the_info_again(conn, scratch_dsn, folder):
    """Presence is the key's: one left without node_info.json is kept, never
    written over, whatever the database holds — and gets the info a random
    identity has, from the key, so P2P runs on its node id."""
    node = node_identity.generate_identity()
    (folder / "node_info.json").unlink()
    key = (folder / "node_ed25519.key").read_bytes()
    for _ in range(2):
        assert node_identity.ensure_identity(UNREACHABLE) == "present"
    with pytest.raises(FileExistsError):
        node_identity.generate_identity()
    assert (folder / "node_ed25519.key").read_bytes() == key
    assert json.loads((folder / "node_info.json").read_text()) == {
        "node_id": node, "public_key_hex": node, "algorithm": "Ed25519"}
    assert node_identity.get_node_id() == node and node_identity.get_account_info() is None


def test_no_identity_over_no_seal_of_this_nodes_own_gets_a_new_one(conn, scratch_dsn, folder):
    """Its own analysis unsealed, another author's sealed under an imported
    source and its canon rows imported: nothing says the node ever had an
    identity. The info an identity left without its key is moved aside."""
    with conn.cursor() as cur:
        for kind in ("segments", "features", "instruments"):
            _record(cur, kind, _source(cur, _track(cur)))
            _record(cur, kind, _source(cur, _track(cur), imported=True), SEAL)
        for kind in ("albums", "album_tracks", "track_mbids"):
            _canon(cur, kind, imported=True)
    folder.mkdir(parents=True)
    (folder / "node_info.json").write_text('{"username": "gone"}')
    assert node_identity.ensure_identity(scratch_dsn) == "minted"
    assert (folder / "node_ed25519.key").exists()
    info = json.loads((folder / "node_info.json").read_text())
    assert "username" not in info and _recorded(conn) == info["public_key_hex"]
    [aside] = folder.glob("replaced-*")
    assert json.loads((aside / "node_info.json").read_text()) == {"username": "gone"}
    assert [p.name for p in folder.iterdir() if p.name.startswith(".")] == []   # no key left half-named


@pytest.mark.parametrize("kind", ["segments", "features", "instruments"])
@pytest.mark.parametrize("under", ["own", "none"])
def test_no_identity_over_seals_of_its_own_gets_none_until_the_owner_chooses(
        conn, scratch_dsn, folder, kind, under):
    with conn.cursor() as cur:
        _record(cur, kind, _source(cur, _track(cur)) if under == "own" else None, SEAL)
        cur.execute(node_identity.SEALED_FIRST_HAND_SQL)
        assert cur.fetchone()[0] is True                 # the cutover's check, the same arm
    assert node_identity.ensure_identity(scratch_dsn) == "missing"
    assert not (folder / "node_info.json").exists()
    assert not (folder / "node_ed25519.key").exists()
    assert node_identity.ensure_identity(scratch_dsn) == "missing"   # every start, the same
    node_identity.generate_identity()                                 # the owner's choice
    assert node_identity.ensure_identity(scratch_dsn) == "present"


@pytest.mark.parametrize("kind", ["albums", "album_tracks", "track_mbids"])
def test_a_seal_on_its_own_canon_says_the_node_had_an_identity(conn, scratch_dsn, folder, kind):
    """The canon's seals are no analysis: the cutover's check leaves them out."""
    with conn.cursor() as cur:
        _canon(cur, kind)
        cur.execute(node_identity.SEALED_FIRST_HAND_SQL)
        assert cur.fetchone()[0] is False
        cur.execute(node_identity.OWN_SEALS_BY_SQL, {"key": SEAL[0]})
        assert cur.fetchone()[0] is True
        cur.execute(node_identity.OWN_SEALS_BY_SQL, {"key": "cd" * 32})
        assert cur.fetchone()[0] is False
    assert node_identity.ensure_identity(scratch_dsn) == "missing"
    assert not (folder / "node_ed25519.key").exists()


def test_a_witness_statement_of_its_own_says_the_node_had_an_identity(conn):
    """Rolled back: a witness row is evidence, never deleted."""
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(node_identity.OWN_SEALS_SQL)
            assert cur.fetchone()[0] is False
            cur.execute("""INSERT INTO p2p_witness (subject, cert_payload, cert_sig, issuer, cert_v,
                                                    issued_at, method, difficulty, params_version,
                                                    channel, author_pubkey, signature)
                           VALUES (%s, 'x', %s, %s, 4, now(), 'pow', 32, 1, 'probe', %s, %s)""",
                        ("cd" * 32, "ef" * 64, "12" * 32, *SEAL))
            cur.execute(node_identity.OWN_SEALS_SQL)
            assert cur.fetchone()[0] is True
            cur.execute(node_identity.OWN_SEALS_BY_SQL, {"key": SEAL[0]})
            assert cur.fetchone()[0] is True
    finally:
        conn.rollback()
        conn.autocommit = True


def _recorded(conn):
    with conn.cursor() as cur:
        cur.execute(f"SELECT {node_identity.SIGNING_KEY_SQL}")
        return cur.fetchone()[0]


def test_a_signing_key_of_record_says_the_node_had_an_identity(conn, scratch_dsn, folder):
    with conn.cursor() as cur:
        cur.execute(node_identity.RECORD_SIGNING_KEY_SQL, {"key": json.dumps("cd" * 32)})
    assert node_identity.ensure_identity(scratch_dsn) == "missing"
    assert not (folder / "node_ed25519.key").exists()


def test_the_key_its_last_p2p_run_held_says_the_node_had_an_identity(conn, scratch_dsn, folder):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO user_settings (key, value) VALUES ('p2p.bound_identity', %s)",
                    (json.dumps("cd" * 32),))
    assert node_identity.ensure_identity(scratch_dsn) == "missing"
    assert not (folder / "node_ed25519.key").exists()


def test_a_key_that_lands_between_the_check_and_the_mint_stays(conn, scratch_dsn, folder,
                                                               monkeypatch):
    """A restore or the web setup writing the key while a start checks: the
    start's mint refuses to write over it, and the start takes it."""
    node_identity.generate_identity()
    key = (folder / "node_ed25519.key").read_bytes()
    real, reads = node_identity.has_identity, []

    def read_before_it_landed():
        reads.append(1)
        return real() if len(reads) > 1 else False
    monkeypatch.setattr(node_identity, "has_identity", read_before_it_landed)
    assert node_identity.ensure_identity(scratch_dsn) == "present"
    assert (folder / "node_ed25519.key").read_bytes() == key
    assert _recorded(conn) == _key_on_disk(folder)       # the key that landed is the one on record


def test_a_database_that_cannot_be_read_decides_nothing(folder):
    with pytest.raises(psycopg2.OperationalError):
        node_identity.ensure_identity(UNREACHABLE)
    assert not (folder / "node_info.json").exists()


# ---------------------------------------------------------------------------
# The settling: one operation at a time, each completion its own
# ---------------------------------------------------------------------------

class _Launcher:
    """The launcher's two threads, played in a chosen order: work handed to
    `run` waits in `threads`, callbacks handed to `ui` in `queue`, and what
    the callbacks were told lands in `said`."""

    def __init__(self, dsn):
        self.threads, self.queue, self.said = [], [], []
        self.closing = False
        self.settling = node_identity.IdentitySettling(
            lambda: dsn, self.threads.append, self.queue.append, lambda: self.closing,
            ready=lambda: self.said.append("ready"),
            missing=lambda: self.said.append("missing"),
            unsettled=lambda e: self.said.append(("unsettled", type(e).__name__)),
            failed=lambda e: self.said.append(("failed", type(e).__name__)))

    def thread(self):
        self.threads.pop(0)()

    def drain(self):
        while self.queue:
            self.queue.pop(0)()


def test_a_backend_start_while_the_owners_choice_is_made_checks_nothing(conn, scratch_dsn, folder):
    """The click, then a Scan's backend restart before the mint lands: the
    restart's check is refused, one key is made, P2P is told once."""
    with conn.cursor() as cur:
        _record(cur, "features", _source(cur, _track(cur)), SEAL)
    node = _Launcher(scratch_dsn)
    assert node.settling.check()
    node.thread()
    node.drain()
    assert node.said == ["missing"]
    assert node.settling.make_new()                    # the owner's click
    assert not node.settling.check()                   # the restart, meanwhile
    node.thread()
    assert node.threads == []
    key = (folder / "node_ed25519.key").read_bytes()
    node.drain()
    assert node.said == ["missing", "ready"] and _recorded(conn) == _key_on_disk(folder)
    assert node.settling.check()                       # the next start's, after it
    node.thread()
    node.drain()
    assert node.said[-1] == "ready" and (folder / "node_ed25519.key").read_bytes() == key


def test_the_owners_choice_is_refused_while_a_check_runs(conn, scratch_dsn, folder):
    with conn.cursor() as cur:
        _record(cur, "features", _source(cur, _track(cur)), SEAL)
    node = _Launcher(scratch_dsn)
    node.settling.check()
    node.thread()
    node.drain()
    assert node.settling.check()                       # a restart's check, the notice still up
    assert not node.settling.make_new()
    node.thread()
    node.drain()
    assert node.said == ["missing", "missing"]
    assert not (folder / "node_ed25519.key").exists()
    assert node.settling.make_new()                    # tried again, once the check is over
    node.thread()
    node.drain()
    assert node.said[-1] == "ready" and (folder / "node_ed25519.key").exists()


def test_a_choice_made_after_the_identity_came_back_keeps_it(conn, scratch_dsn, folder):
    with conn.cursor() as cur:
        _record(cur, "features", _source(cur, _track(cur)), SEAL)
    node = _Launcher(scratch_dsn)
    node.settling.check()
    node.thread()
    node.drain()
    node_identity.generate_identity()                  # a copy of the folder put back
    key = (folder / "node_ed25519.key").read_bytes()
    node.settling.make_new()
    node.thread()
    node.drain()
    assert node.said[-1] == "ready" and (folder / "node_ed25519.key").read_bytes() == key
    assert _recorded(conn) == _key_on_disk(folder)       # the key back is the one on record


def test_a_completion_while_the_launcher_quits_calls_nothing(conn, scratch_dsn, folder):
    with conn.cursor() as cur:
        _record(cur, "features", _source(cur, _track(cur)), SEAL)
    node = _Launcher(scratch_dsn)
    node.settling.check()
    node.thread()
    node.closing = True                                # Quit, before the answer lands
    node.drain()
    assert node.said == [] and not node.settling.busy


def test_a_check_that_decides_nothing_lets_the_next_one_run(folder):
    node = _Launcher(UNREACHABLE)
    node.settling.check()
    node.thread()
    node.drain()
    assert node.said == [("unsettled", "OperationalError")] and not node.settling.busy
    assert node.settling.check()


def test_a_check_that_fails_is_released_and_raised(conn, scratch_dsn, folder, monkeypatch):
    def broken(_dsn):
        raise RuntimeError("the check broke")
    monkeypatch.setattr(node_identity, "ensure_identity", broken)
    node = _Launcher(scratch_dsn)
    node.settling.check()
    with pytest.raises(RuntimeError):                  # for the thread's hook to log
        node.thread()
    node.drain()
    assert node.said == [("unsettled", "RuntimeError")] and not node.settling.busy


def test_a_new_identity_that_could_not_be_made_is_said_and_can_be_chosen_again(
        conn, scratch_dsn, folder, monkeypatch):
    real = node_identity.generate_identity

    def full_disk(_dsn=None):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(node_identity, "generate_identity", full_disk)
    node = _Launcher(scratch_dsn)
    node.settling.make_new()
    node.thread()
    node.drain()
    assert node.said == [("failed", "OSError")] and not node.settling.busy
    monkeypatch.setattr(node_identity, "generate_identity", real)
    assert node.settling.make_new()
    node.thread()
    node.drain()
    assert node.said[-1] == "ready" and (folder / "node_ed25519.key").exists()


def test_the_owners_new_identity_is_on_record_before_it_is_placed(conn, scratch_dsn, folder):
    node = _Launcher(UNREACHABLE)                      # the database away: nothing is placed
    node.settling.make_new()
    node.thread()
    node.drain()
    assert node.said == [("failed", "OperationalError")] and not (folder / "node_ed25519.key").exists()
    node = _Launcher(scratch_dsn)
    node.settling.make_new()
    node.thread()
    node.drain()
    assert node.said == ["ready"] and _recorded(conn) == _key_on_disk(folder)


def test_a_crash_before_the_key_is_linked_leaves_no_key_beside_another_identitys_info(
        folder, monkeypatch):
    """The key is written under a temporary name and linked into place after
    the info of the identity that is gone was moved: a crash between leaves
    that info aside and no key, never the two together."""
    folder.mkdir(parents=True)
    (folder / "node_info.json").write_text('{"username": "gone"}')

    def power_cut(*_a):
        raise SystemExit("power cut")
    monkeypatch.setattr(node_identity, "_link_exclusive", power_cut)
    with pytest.raises(SystemExit):
        node_identity.generate_identity()
    assert not (folder / "node_ed25519.key").exists() and not (folder / "node_info.json").exists()
    [aside] = folder.glob("replaced-*")
    assert json.loads((aside / "node_info.json").read_text()) == {"username": "gone"}


def test_a_key_that_lands_before_the_link_is_kept_and_the_info_goes_back(folder, monkeypatch):
    folder.mkdir(parents=True)
    (folder / "node_info.json").write_text('{"username": "gone"}')
    real = node_identity._link_exclusive

    def another_writer_first(tmp, key, pem):
        key.write_bytes(b"the key that landed")
        real(tmp, key, pem)
    monkeypatch.setattr(node_identity, "_link_exclusive", another_writer_first)
    with pytest.raises(FileExistsError):
        node_identity.generate_identity()
    assert (folder / "node_ed25519.key").read_bytes() == b"the key that landed"
    assert json.loads((folder / "node_info.json").read_text()) == {"username": "gone"}
    assert list(folder.glob("replaced-*")) == []
    assert [p.name for p in folder.iterdir() if p.name.startswith(".")] == []


def test_a_filesystem_without_hard_links_gets_the_key_created_exclusively(folder, monkeypatch):
    def no_links(_src, _dst):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(node_identity.os, "link", no_links)
    node = node_identity.generate_identity()
    assert node_identity.has_identity() and _key_on_disk(folder) == node
    with pytest.raises(FileExistsError):
        node_identity.generate_identity()
    assert _key_on_disk(folder) == node


def test_a_key_whose_info_cannot_be_moved_aside_is_not_placed(folder, monkeypatch):
    """Beside a new key the old info would name the identity that is gone:
    when it cannot be moved, no key is placed and the old info stays."""
    folder.mkdir(parents=True)
    (folder / "node_info.json").write_text('{"username": "gone"}')

    def refuse(self, target):
        raise PermissionError(13, "in use")
    monkeypatch.setattr(type(folder), "rename", refuse)
    with pytest.raises(PermissionError):
        node_identity.generate_identity()
    assert not (folder / "node_ed25519.key").exists()
    assert json.loads((folder / "node_info.json").read_text()) == {"username": "gone"}


# ---------------------------------------------------------------------------
# The launcher's side: its own methods, run on stand-in widgets
# ---------------------------------------------------------------------------

class _Widget:
    """What the identity notice and the status line touch of a widget."""

    def __init__(self, parent=None, **options):
        self.options, self.alive, self.text, self.children = dict(options), True, "", []
        if isinstance(parent, _Widget):
            parent.children.append(self)

    def says(self) -> str:
        """Every text inserted in this widget and the ones inside it."""
        return self.text + "".join(child.says() for child in self.children)

    def configure(self, **options):
        self.options.update(options)

    def cget(self, name):
        return self.options.get(name)

    def protocol(self, _name, fn):
        self.options["close"] = fn

    def insert(self, _index, text):
        self.text += text

    def winfo_exists(self):
        return self.alive

    def destroy(self):
        self.alive = False

    def pack(self, *_a, **_k): pass
    def title(self, *_a): pass
    def geometry(self, *_a): pass
    def resizable(self, *_a): pass
    def transient(self, *_a): pass
    def lift(self): pass


_NOTICE_METHODS = ("_identity_ready", "_identity_missing", "_identity_unsettled", "_identity_failed",
                   "_show_identity_notice", "_build_identity_notice", "_set_status", "_start_p2p")
_NOTICE_WORDS = ("IDENTITY_MISSING", "NEW_IDENTITY_COSTS", "IDENTITY_MISSING_STATUS",
                 "IDENTITY_UNCHECKED_STATUS", "IDENTITY_MISSING_HINT")


def _launcher(dsn):
    """desktop/launcher.py's identity methods — read out of its source, since
    customtkinter is not in this image — on an app of stand-ins: the
    settling's threads and the UI queue as _Launcher plays them, P2P starts
    counted, the window's raises recorded."""
    import ast
    import logging
    import textwrap
    import types
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "desktop" / "launcher.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    app = types.SimpleNamespace(p2p_starts=0, raised=[], dsn=dsn)
    ns = {"ctk": types.SimpleNamespace(CTkToplevel=_Widget, CTkFrame=_Widget, CTkLabel=_Widget,
                                       CTkButton=_Widget, CTkTextbox=_Widget,
                                       CTkFont=lambda **_k: None),
          "logger": logging.getLogger("launcher"),
          "threading": types.SimpleNamespace(
              Thread=lambda **_k: types.SimpleNamespace(
                  start=lambda: setattr(app, "p2p_starts", app.p2p_starts + 1)))}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in _NOTICE_WORDS):
            exec(ast.get_source_segment(source, node), ns)
    [cls] = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LauncherApp"]
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name in _NOTICE_METHODS:
            exec(textwrap.dedent(ast.get_source_segment(source, node)), ns)
            setattr(app, node.name, types.MethodType(ns[node.name], app))
    assert all(hasattr(app, name) for name in _NOTICE_METHODS)
    app.words = ns
    app._shutting_down = app._node_down = app._p2p_starting = app._identity_asked = False
    app.p2p_manager = app._identity_notice = None
    app._status_dot, app._progress_text = _Widget(), _Widget()
    app._status_text = _Widget(text="All services running")
    app.deiconify = lambda: app.raised.append("deiconify")
    app.lift = lambda: app.raised.append("lift")
    node = _Launcher(dsn)
    node.settling = app._identity = node_identity.IdentitySettling(
        lambda: app.dsn, node.threads.append, node.queue.append, lambda: app._shutting_down,
        ready=app._identity_ready, missing=app._identity_missing,
        unsettled=app._identity_unsettled, failed=app._identity_failed)
    return app, node


def _checked(node):
    assert node.settling.check()
    node.thread()
    node.drain()


def test_the_launcher_asks_once_a_start_out_of_the_tray_and_says_why_the_network_is_off(
        conn, scratch_dsn, folder):
    with conn.cursor() as cur:
        _record(cur, "features", _source(cur, _track(cur)), SEAL)
    app, node = _launcher(scratch_dsn)
    _checked(node)
    notice = app._identity_notice
    assert notice is not None and app.raised == ["deiconify", "lift"]
    assert str(folder) in notice.says() and app.words["NEW_IDENTITY_COSTS"] in notice.says()
    assert app._status_text.cget("text") == app.words["IDENTITY_MISSING_STATUS"]
    assert app._progress_text.cget("text") == app.words["IDENTITY_MISSING_HINT"]
    notice.options["close"]()                          # Not now
    app._status_text.configure(text="All services running")   # a backend start's own line
    _checked(node)
    assert app._identity_notice is None and app.raised == ["deiconify", "lift"]
    assert app._status_text.cget("text") == app.words["IDENTITY_MISSING_STATUS"]
    assert app.p2p_starts == 0 and not (folder / "node_ed25519.key").exists()


def test_the_launchers_new_identity_starts_p2p_once_and_clears_the_status_line(
        conn, scratch_dsn, folder):
    with conn.cursor() as cur:
        _record(cur, "features", _source(cur, _track(cur)), SEAL)
    app, node = _launcher(scratch_dsn)
    _checked(node)
    notice = app._identity_notice
    assert node.settling.check()                       # a restart's check, the notice up
    notice.make.options["command"]()                   # the owner's click, refused meanwhile
    assert "running" in notice.caption.cget("text") and node.threads and len(node.threads) == 1
    node.thread()
    node.drain()
    notice.make.options["command"]()
    assert notice.make.cget("state") == "disabled"
    node.thread()
    node.drain()
    assert (folder / "node_ed25519.key").exists() and not notice.alive
    assert app._status_text.cget("text") == "All services running" and app.p2p_starts == 1


def test_a_check_that_could_not_read_the_database_leaves_the_notice_and_its_choice(
        conn, scratch_dsn, folder):
    with conn.cursor() as cur:
        _record(cur, "features", _source(cur, _track(cur)), SEAL)
    app, node = _launcher(scratch_dsn)
    _checked(node)
    notice = app._identity_notice
    app.dsn = UNREACHABLE                              # a restart's check, the database away
    _checked(node)
    assert app._status_text.cget("text") == app.words["IDENTITY_UNCHECKED_STATUS"] and notice.alive
    notice.make.options["command"]()                   # nothing is made off the record
    node.thread()
    node.drain()
    assert "could not be made" in notice.caption.cget("text") and app.p2p_starts == 0
    assert not (folder / "node_ed25519.key").exists()
    app.dsn = scratch_dsn                              # the database back
    notice.make.options["command"]()
    node.thread()
    node.drain()
    assert app._status_text.cget("text") == "All services running" and app.p2p_starts == 1


def test_the_launcher_says_a_new_identity_that_could_not_be_made_in_the_notice(
        conn, scratch_dsn, folder, monkeypatch):
    def full_disk(_dsn=None):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(node_identity, "generate_identity", full_disk)
    with conn.cursor() as cur:
        _record(cur, "features", _source(cur, _track(cur)), SEAL)
    app, node = _launcher(scratch_dsn)
    _checked(node)
    notice = app._identity_notice
    notice.make.options["command"]()
    notice.options["close"]()                          # closed while it is being made
    node.thread()
    node.drain()
    again = app._identity_notice
    assert again is not None and again is not notice
    assert "No space left on device" in again.caption.cget("text")
    assert again.make.cget("state") == "normal" and app.p2p_starts == 0


def test_a_check_that_lands_while_the_services_are_down_leaves_their_line_and_starts_nothing(
        conn, scratch_dsn, folder):
    with conn.cursor() as cur:
        _record(cur, "features", _source(cur, _track(cur)), SEAL)
    app, node = _launcher(scratch_dsn)
    down = "Backend crashed — restarting..."
    assert node.settling.check()
    node.thread()
    app._node_down = True                              # the backend went down meanwhile
    app._status_text.configure(text=down)
    node.drain()
    assert app._identity_notice is None and not app._identity_asked
    assert app._status_text.cget("text") == down
    app._node_down = False                             # their next start asks
    app._status_text.configure(text="All services running")
    _checked(node)
    notice = app._identity_notice
    assert notice is not None and app._identity_asked
    node_identity.generate_identity()                  # the folder put back
    assert node.settling.check()
    node.thread()
    app._node_down = True
    app._status_text.configure(text=down)
    node.drain()
    assert app.p2p_starts == 0 and app._status_text.cget("text") == down and not notice.alive
    app._node_down = False
    app._status_text.configure(text="All services running")
    _checked(node)
    assert app.p2p_starts == 1


def test_the_launcher_quitting_shows_nothing_and_starts_nothing(conn, scratch_dsn, folder):
    app, node = _launcher(scratch_dsn)
    assert node.settling.check()
    node.thread()                                      # no seal: minted
    app._shutting_down = True
    node.drain()
    assert app.p2p_starts == 0 and app._identity_notice is None
