"""Write backend/rights_societies/rights_societies.json, the names of the
rights societies every node's scanner never reads as an artist (TONO, GEMA,
JASRAC, BMI, ASCAP, SACEM, … — CANON-CLASSICAL-DESIGN §4.2).

The list is MusicBrainz's own (CC0): every label of the type "Rights
Society" with its aliases, each folded by uuid_utils.normalize, read from a
core dump archive (mbdump-*.tar.bz2, its checksum and signature verified as
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
                    labels[c[0]] = (c[2], c[10])
                else:
                    aliases.append((c[1], c[2]))
            want.discard(m.name)
            if not want:
                break
    proc.kill()
    society = next(k for k, name in kinds.items() if name == "Rights Society")
    ids = {lid for lid, (_name, kind) in labels.items() if kind == society}
    names = {labels[lid][0] for lid in ids} | {name for lid, name in aliases if lid in ids}
    version = Path(archive).name.removeprefix("mbdump-").removesuffix(".tar.bz2")
    return {"source": "MusicBrainz labels of type Rights Society, with their aliases (CC0)",
            "dump": version, "names": sorted({normalize(n) for n in names} - {""})}


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
