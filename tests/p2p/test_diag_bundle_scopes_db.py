"""A support bundle's structural scopes — system, settings, p2p, jobs,
events — are facts about the node, never where its files are, whom it talks
to or who its owner is (desktop/diag_bundle.collect): the music folder, the
reachability's addresses, the HQPlayer's address, an email in a setting, a
node's name and the Last.fm username stay out, whatever the settings rows,
config.json or the node's own API answer. A real PostgreSQL built from the
migrations; the node's backend API is a fake of it. Skipped without a cluster."""

import io
import json
import tarfile
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from desktop import diag_bundle  # noqa: E402

PERSONAL = ("someone", "Someone", "8.8.8.8", "192.0.2", "example.com", "pixel.lan")


class _NodeApi:
    """The node's own backend API, answering what the facts ask."""

    def _get_json(self, path, timeout=None):
        return {
            "/api/settings/sync": {"reachability_detail": "wan 8.8.8.8:24001 via 192.0.2.1"},
            "/api/settings/library": {"music_path": "/home/someone/Music", "total_tracks": 3},
            "/api/settings/musicbrainz/status": {"state": "none"},
            "/health": {"status": "healthy", "checks": {
                "music_library": {"path": "/home/someone/Music", "exists": True}}},
        }.get(path)

    def get_stats(self):
        return {"tracks": 3}

    def scan_status(self):
        return {"current_file": "/home/someone/Music/A Band/x.flac"}

    def enrich_status(self):
        return {"state": "idle"}


def test_the_structural_scopes_name_no_path_address_or_person(scratch_dsn, tmp_path, monkeypatch):
    monkeypatch.setattr(diag_bundle, "_backend_client", lambda config: _NodeApi())
    conn = psycopg2.connect(scratch_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DELETE FROM diag_events")
        for key, value in (("p2p.reachability_detail", "wan 8.8.8.8:24001 via 192.0.2.1"),
                           ("hqplayer.host", "Someones-Desktop"),
                           ("library.watch_folder", "/home/someone/Music"),
                           ("support.contact", "someone@example.com"),
                           ("output.dlna_renderer", {"udn": "uuid:5f9ec1b3-ed3a-4f0d-8d84-1",
                                                     "location": "http://pixel.lan:8200/d.xml",
                                                     "name": "Someone's Phone"}),
                           ("output.dlna_pinned", [{"udn": "uuid:5f9ec1b3-ed3a-4f0d-8d84-1",
                                                    "name": "Someone's Phone"}]),
                           ("output.local_device", "WASAPI::Someone's Headphones")):
            cur.execute("INSERT INTO user_settings (key, value) VALUES (%s, %s) "
                        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                        (key, json.dumps(value)))
        # an event kept before the recorder scrubbed what it stored
        cur.execute("INSERT INTO diag_events (kind, detail) VALUES ('sync.failed', %s)",
                    (json.dumps({"error": "peer 192.0.2.4 for someone@example.com"}),))
    conn.close()
    config = {"music_path": "/home/someone/Music", "lastfm": {"username": "someone"},
              "p2p": {"node_name": "someone-pc", "listen_port": 24001},
              "hqplayer": {"host": "192.0.2.9", "port": 4321}, "ports": {"web": 18000}}
    warrant = {"id": str(uuid.uuid4()), "scopes": ["system", "settings", "p2p", "jobs", "events"],
               "since": None}

    data = diag_bundle.collect(db_dsn=scratch_dsn, data_dir=tmp_path, config=config,
                               warrant=warrant, granted=[], node_pubkey="ab" * 32,
                               extra={"reachability": {"wan": "8.8.8.8", "lan": "192.0.2.3"}})

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = {m.name: tar.extractfile(m).read().decode("utf-8") for m in tar.getmembers()}
    assert {"system.json", "settings.json", "p2p.json", "jobs.json", "events.json"} <= set(members)
    assert json.loads(members["manifest.json"])["errors"] == {}
    for name, text in members.items():
        for personal in PERSONAL:
            assert personal not in text, (name, personal)
    settings = json.loads(members["settings.json"])
    assert settings["config"]["lastfm"] == {"authorized": True}
    assert settings["user_settings"]["hqplayer.host"]["value"] == "another machine"
    assert settings["user_settings"]["output.local_device"]["value"] == "WASAPI"
    assert settings["user_settings"]["output.dlna_renderer"]["value"] == {
        "udn": "uuid:5f9ec1b3-ed3a-4f0d-8d84-1", "location": "http://[host]:8200/d.xml"}
