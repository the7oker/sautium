"""A node asks MusicBrainz's web service by MBID only: the client has no name
search and no by-name browse of an artist's albums — the path a node with
neither the dump nor slices used to take for its discographies, under a
person's address — and the audit tools that renamed and merged on such
answers are gone."""

import importlib.util
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_the_web_service_client_asks_by_mbid_only():
    import musicbrainz
    assert not hasattr(musicbrainz, "search_artist")
    assert not hasattr(musicbrainz, "fetch_album_release_groups")


def test_the_name_audit_tools_are_gone():
    for module in ("mb_audit", "mb_apply", "mb_canonicalize"):
        assert importlib.util.find_spec(module) is None, module
