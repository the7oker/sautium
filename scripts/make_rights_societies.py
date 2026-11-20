"""Write backend/rights_societies/rights_societies.json, the names of the
rights societies every node's scanner never reads as an artist (TONO, GEMA,
JASRAC, BMI, ASCAP, SACEM, … — CANON-CLASSICAL-DESIGN §4.2).

The list is MusicBrainz's own (CC0): every label of the type "Rights
Society" with its aliases, each folded by uuid_utils.normalize — less the
labels MusicBrainz types so that are no rights society (NOT_SOCIETIES, each by
its MBID with the reason; CANON-CLASSICAL-DESIGN §12.9) — read from a core
dump archive (mbdump-*.tar.bz2, its checksum and signature verified as
mb_dump_load verifies them). The dump node's tables do not hold labels, and
a node without the dump needs the list too, so the file ships with the
release; this script only says how it was made.

    python scripts/make_rights_societies.py <mbdump-VERSION.tar.bz2>         # writes the file
    python scripts/make_rights_societies.py <archive> --check                # compares with the committed file
"""

import argparse
import json
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from uuid_utils import normalize  # noqa: E402

OUT = ROOT / "backend" / "rights_societies" / "rights_societies.json"
MEMBERS = ("mbdump/label_type", "mbdump/label", "mbdump/label_alias")

# Labels MusicBrainz types "Rights Society" that collect no rights: their
# names and aliases would read as a society in a publishing credit. Read from
# the 2026-11-11 dump (the name, its disambiguation, its label code).
NOT_SOCIETIES = {
    "744b5733-3512-4bb0-89b8-f94cc1c56799": ("Limelight",
                                             "a licensing service (disambiguation \"licensing\"), no society"),
    "4bbca89e-288d-40ec-ac8a-4c6568e1773d": ("Azure Voice",
                                             "a record label: label code LC 205244, \"Voice of Africa\""),
    "8f5f60be-647e-406c-91b0-84ba462db7d0": ("Raplarue", "a casting show (disambiguation \"Casting Show\")"),
    "7e2809d0-cd83-4b3a-aa49-178f33b0332f": ("The Bridgeman Art Library", "a picture library"),
    "241c8fa8-dd3f-4a2c-9640-acdbcd9437eb": ("Writers' Guild of Great Britain",
                                             "a writers' trade union, no music rights society"),
    "1ac78115-da2a-4e9b-b42f-3d2fafafe3e2": ("Pulse-8 Records Ltd", "a record label"),
    "8cbcc38d-d2da-40cd-b391-77a2da18e75d": ("Keep a/c",
                                             "\"Keep Account\": no society goes by it; no disambiguation, area or label code"),
}


def read(archive: str) -> dict:
    """{"dump": version, "names": sorted folded names}. MB's columns: label_type
    (id, name, …), label (id, gid, name, …, type at 10), label_alias (id, label,
    name, …)."""
    want = set(MEMBERS)
    kinds, labels, aliases = {}, {}, []
    proc = subprocess.Popen(["lbzip2", "-dc", archive], stdout=subprocess.PIPE, bufsize=1 << 20)
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
        for m in tar:
            if m.name not in want:
                continue
            for raw in tar.extractfile(m):
                c = raw.decode().rstrip("\n").split("\t")
                if m.name == "mbdump/label_type":
                    kinds[c[0]] = c[1]
                elif m.name == "mbdump/label":
                    labels[c[0]] = (c[2], c[10], c[1])
                else:
                    aliases.append((c[1], c[2]))
            want.discard(m.name)
            if not want:
                break
    proc.kill()
    society = next(k for k, name in kinds.items() if name == "Rights Society")
    ids = {lid for lid, (_name, kind, gid) in labels.items() if kind == society and gid not in NOT_SOCIETIES}
    names = {labels[lid][0] for lid in ids} | {name for lid, name in aliases if lid in ids}
    version = Path(archive).name.removeprefix("mbdump-").removesuffix(".tar.bz2")
    return {"source": "MusicBrainz labels of type Rights Society, with their aliases (CC0)",
            "dump": version,
            "not_societies": [{"mbid": gid, "name": name, "why": why}
                              for gid, (name, why) in sorted(NOT_SOCIETIES.items(), key=lambda x: x[1][0])],
            "names": sorted({normalize(n) for n in names} - {""})}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("archive")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    made = read(args.archive)
    if args.check:
        same = json.loads(OUT.read_text(encoding="utf-8"))["names"] == made["names"]
        print("same" if same else "differs")
        return 0 if same else 1
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(made, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(made['names'])} names from {made['dump']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
