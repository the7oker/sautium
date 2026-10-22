#!/usr/bin/env bash
# Copy the published HQPlayer atlas's app subset into the backend: the atlas's
# atlas/v1/public/app/hqp-atlas.json in sautium-private becomes
# backend/hqp_atlas.json, the file backend/hqp_atlas.py reads, and nothing
# else is copied — never full/, never the curation.
#
#   scripts/sync-atlas.sh [--into DIR]
#   SAUTIUM_PRIVATE_DIR=<dir> scripts/sync-atlas.sh
#
# The source is ${SAUTIUM_PRIVATE_DIR:-<this repository>/../sautium-private}/
# atlas/v1/public, the published set alone. Every file there must be the one
# public/manifest.json lists, by its size and sha256: a file or a link the
# manifest does not list, or a listed file missing or different, refuses the
# sync. So does a set without the app subset (no release dated yet), one
# backend/hqp_atlas.py would refuse, and one whose releases are not each dated
# as the manifest dates them — an entry without a published date among them.
# The bytes checked are the bytes copied. Beside the file goes
# hqp_atlas.version.json: the dataset, its schema, the manifest's sha256, the
# copied file's, the commit the manifest names its code by and its dated
# releases. Nothing is written until every check has passed; each file is
# replaced whole, the data first — a version file is the data's when its
# app_sha256 is. --into writes the two files into DIR instead of backend/.
#
# It publishes nothing: both files stay out of git (.gitignore) until the
# release that ships them.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
into="$root/backend"
case "${1:-}" in
    --into) into=${2:?"usage: $0 [--into DIR]"} ;;
    "") ;;
    *) echo "usage: $0 [--into DIR]" >&2; exit 2 ;;
esac
src="${SAUTIUM_PRIVATE_DIR:-$root/../sautium-private}/atlas/v1/public"
[ -f "$src/manifest.json" ] || { echo "sync-atlas: no $src/manifest.json (SAUTIUM_PRIVATE_DIR)" >&2; exit 1; }
[ -d "$into" ] || { echo "sync-atlas: no directory $into" >&2; exit 1; }

python3 -I -B - "$src" "$into" "$root/backend" <<'EOF'
import hashlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

src, into = Path(sys.argv[1]), Path(sys.argv[2])
APP = "app/hqp-atlas.json"


def refuse(why):
    sys.exit(f"sync-atlas: {why}; nothing was copied")


manifest_bytes = (src / "manifest.json").read_bytes()
manifest = json.loads(manifest_bytes)
if manifest.get("dataset") != "hqp-atlas":
    refuse(f"{src}/manifest.json names no hqp-atlas set")
missing = [key for key in ("schema", "code", "releases", "outputs") if key not in manifest]
if missing:
    refuse(f"{src}/manifest.json holds no {', '.join(missing)}")
listed = {o["path"]: o for o in manifest["outputs"]}
for path in sorted(src.rglob("*")):
    rel = path.relative_to(src).as_posix()
    # A link is an entry of its own, whatever it points at: the publish step writes none
    if (path.is_symlink() or not path.is_dir()) and rel != "manifest.json" and rel not in listed:
        refuse(f"{rel} is in public/ and not in its manifest")
if APP not in listed:
    refuse(f"the published set holds no {APP}: no release is dated")
for rel, o in listed.items():
    if not (src / rel).is_file():
        refuse(f"{rel}, which the manifest lists, is missing")
    data = (src / rel).read_bytes()
    if len(data) != o["bytes"] or hashlib.sha256(data).hexdigest() != o["sha256"]:
        refuse(f"{rel} is not the file the manifest names (size or sha256)")
    if rel == APP:
        app = data          # the bytes checked are the bytes copied

dated = {f"{r['engine']}/{r['family']}": r["published"] for r in manifest["releases"] if r["published"]}
version = json.dumps({
    "dataset": manifest["dataset"],
    "schema": manifest["schema"],
    "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    "app_sha256": hashlib.sha256(app).hexdigest(),
    "code": manifest["code"]["commit"],
    "releases": manifest["releases"],
}, indent=1) + "\n"
sys.path.insert(0, sys.argv[3])
logging.basicConfig(format="%(message)s")
import hqp_atlas  # noqa: E402

# The reader the node runs takes the file whole or not at all, read here from
# the bytes checked, put aside outside the tree; a refusal is logged in one line
# naming a place in the file
with tempfile.TemporaryDirectory() as aside:
    (Path(aside) / "hqp_atlas.json").write_bytes(app)
    if hqp_atlas.read(Path(aside) / "hqp_atlas.json", None) is None:
        refuse(f"backend/hqp_atlas.py refuses {APP} (above)")
# A shipped file holds dated releases alone, each as the manifest dates it: an
# undated one would still decide which engine the HQPlayer screen shows
for key, date in json.loads(app)["releases"].items():
    if date is None or dated.get(key) != date:
        refuse(f"release {key} is dated {date or 'none'} in the subset and {dated.get(key) or 'none'} in "
               "the manifest")

staged = []
try:
    for name, data in (("hqp_atlas.json", app), ("hqp_atlas.version.json", version.encode())):
        fd, tmp = tempfile.mkstemp(dir=into, prefix=f".{name}.")
        staged.append((tmp, into / name))
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, 0o644)
    for tmp, path in staged:
        os.replace(tmp, path)
finally:
    for tmp, _ in staged:
        if os.path.exists(tmp):
            os.remove(tmp)
releases = ", ".join(f"{r['engine']}/{r['family']} {r['published']}" for r in manifest["releases"])
print(f"sync-atlas: {APP} copied to {into / 'hqp_atlas.json'} ({len(app)} bytes; releases: {releases})")
EOF
