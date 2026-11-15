#!/usr/bin/env bash
# Publish the seed bundle seed_export.py wrote: create the GitHub release
# `seed-v<N>` when it does not exist, upload the bundle as its asset (a
# same-named asset is replaced), then fetch it back over the public URL and
# check the sha256 against the digest seed_export.py wrote beside it
# (<seed dir>/bundle.json), and copy that digest into backend/seed/bundle.json
# — the file every node reads before downloading. Commit it afterwards.
#
# Token: $GITHUB_TOKEN, else `git credential fill` through whatever helper git
# has configured (a one-off `-c credential.helper=…` goes in
# GIT_CONFIG_PARAMETERS on a machine that has none).
set -euo pipefail
export GIT_TERMINAL_PROMPT=0

repo=the7oker/sautium
root=$(cd "$(dirname "$0")/.." && pwd)
seed_dir="${SEED_DIR:-$root/data/seed}"
info="$seed_dir/bundle.json"

# The digest is a file the node wrote (<seed dir> is one of its folders): read
# without following a link, each field held to its form before the shell or
# the tree sees it
read -r version url sha size picks analyzed < <(python3 -I -c '
import json, os, re, stat, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW)
assert stat.S_ISREG(os.fstat(fd).st_mode), "bundle.json is no regular file"
d = json.loads(os.read(fd, 65536))
version, sha, size = int(d["version"]), d["sha256"], int(d["size"])
picks, analyzed = int(d["picks"]), int(d["analyzed_tracks"])
url = f"https://github.com/the7oker/sautium/releases/download/seed-v{version}/seed_v{version}.json.gz"
assert d["url"] == url and re.fullmatch("[0-9a-f]{64}", sha), "bundle.json is not a seed digest"
print(version, url, sha, size, picks, analyzed)' "$info")
[ -n "$sha" ] || { echo "$info is not a seed digest" >&2; exit 1; }
name="seed_v${version}.json.gz"
# The bundle is a file of the node's too: opened once, without following a
# link, into a file of this script's own; that copy is what is checked and
# what is uploaded
held=$(mktemp)
check=$(mktemp)
trap 'rm -f "$held" "$check"' EXIT
python3 -I -c '
import os, shutil, stat, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW)
assert stat.S_ISREG(os.fstat(fd).st_mode), "the bundle is no regular file"
with os.fdopen(fd, "rb") as src, open(sys.argv[2], "wb") as dst:
    shutil.copyfileobj(src, dst)' "$seed_dir/$name" "$held" \
    || { echo "no bundle at $seed_dir/$name — run seed_export.py first" >&2; exit 1; }
echo "$sha  $held" | sha256sum -c --quiet \
    || { echo "bundle.json does not describe $seed_dir/$name — re-run seed_export.py" >&2; exit 1; }

token=${GITHUB_TOKEN:-$(printf 'protocol=https\nhost=github.com\n\n' \
    | git credential fill | sed -n 's/^password=//p')}
[ -n "$token" ] || { echo "no GitHub token (GITHUB_TOKEN or git credential fill)" >&2; exit 1; }
api() {
    curl -sSf -H "Authorization: Bearer $token" -H "Accept: application/vnd.github+json" \
         -H "X-GitHub-Api-Version: 2022-11-28" "$@"
}
field() { python3 -c 'import json, sys; d = json.load(sys.stdin); print(eval(sys.argv[1]))' "$@"; }

tag="seed-v${version}"
release=$(api "https://api.github.com/repos/$repo/releases/tags/$tag" 2>/dev/null || true)
if [ -z "$release" ]; then
    release=$(api -X POST "https://api.github.com/repos/$repo/releases" -d "$(python3 -c '
import json, sys
print(json.dumps({
    "tag_name": sys.argv[1], "target_commitish": "main", "name": "Seed bundle v" + sys.argv[2],
    "body": "Cold-start seed bundle — imported by every node on its first start "
            "(backend/seed_import.py checks it against the sha256 in backend/seed/bundle.json).\n\n"
            "sha256 " + sys.argv[3],
    # /releases/latest must stay the installers release (release-publish.sh):
    # the website reads downloads.json from it.
    "make_latest": "false",
}))' "$tag" "$version" "$sha")")
    echo "created release $tag"
fi
rid=$(field 'd["id"]' <<<"$release")
aid=$(field 'next((a["id"] for a in d.get("assets", []) if a["name"] == sys.argv[2]), "")' "$name" <<<"$release")
if [ -n "$aid" ]; then
    api -X DELETE "https://api.github.com/repos/$repo/releases/assets/$aid"
    echo "replaced the previous $name"
fi
api -X POST -H "Content-Type: application/gzip" --data-binary @"$held" \
    "https://uploads.github.com/repos/$repo/releases/$rid/assets?name=$name" >/dev/null

curl -sSfL -o "$check" "$url"
echo "$sha  $check" | sha256sum -c --quiet
# The tree's copy is written from the fields checked above, never copied: the
# node's file could be a link to any file of the person's
python3 -I -c '
import json, sys
v, url, sha, size, picks, analyzed, out = sys.argv[1:8]
info = {"version": int(v), "url": url, "sha256": sha, "size": int(size), "picks": int(picks),
        "analyzed_tracks": int(analyzed)}
with open(out, "w", encoding="utf-8") as f:
    f.write(json.dumps(info, indent=2) + "\n")' "$version" "$url" "$sha" "$size" "$picks" "$analyzed" \
    "$root/backend/seed/bundle.json"
echo "published $url ($size bytes, sha256 $sha); its digest is in backend/seed/bundle.json — commit it"
