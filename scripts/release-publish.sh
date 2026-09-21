#!/usr/bin/env bash
# Publish a launcher release: the installers desktop/build_windows.py and
# desktop/build_macos.py wrote (the DMG copied over from the Mac that built
# it), plus SHA256SUMS.txt and downloads.json — the file the website's
# download page reads from releases/latest/download/downloads.json. Creates
# the GitHub release v<version> when it does not exist — marked latest and
# NOT a prerelease, because a prerelease drops out of /releases/latest and
# the site would read whatever release is left there — replaces same-named
# assets, then fetches every asset back over the public URL and checks it
# against SHA256SUMS.txt.
#
#   scripts/release-publish.sh [--version V] [--notes FILE] [--dry-run] <artefact>...
#
# --version defaults to desktop/build_common.py VERSION; --notes replaces the
# default release body; --dry-run writes SHA256SUMS.txt and downloads.json
# into dist/ ($DIST_DIR) and stops before the first API call. Token as in
# seed-publish.sh: $GITHUB_TOKEN, else `git credential fill`.
set -euo pipefail
export GIT_TERMINAL_PROMPT=0

repo=the7oker/sautium
root=$(cd "$(dirname "$0")/.." && pwd)
out=${DIST_DIR:-$root/dist}
usage="usage: $0 [--version V] [--notes FILE] [--dry-run] <artefact>..."

version=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$root/desktop/build_common.py")
notes=""
dry_run=0
artefacts=()
while [ $# -gt 0 ]; do
    case "$1" in
        --version) version=$2; shift 2 ;;
        --notes) notes=$2; shift 2 ;;
        --dry-run) dry_run=1; shift ;;
        -h|--help) echo "$usage"; exit 0 ;;
        -*) echo "unknown option $1" >&2; echo "$usage" >&2; exit 2 ;;
        *) artefacts+=("$1"); shift ;;
    esac
done
[ ${#artefacts[@]} -gt 0 ] || { echo "$usage" >&2; exit 2; }
[ -n "$version" ] || { echo "no version in desktop/build_common.py — pass --version" >&2; exit 2; }
[ -z "$notes" ] || [ -f "$notes" ] || { echo "no notes file at $notes" >&2; exit 2; }

for file in "${artefacts[@]}"; do
    [ -f "$file" ] || { echo "no artefact at $file" >&2; exit 1; }
    case "$(basename "$file")" in
        "Sautium-$version-"*.exe|"Sautium-$version-"*.dmg) ;;
        *) echo "$(basename "$file") is not a Sautium-$version-<...>.exe/.dmg installer" >&2; exit 1 ;;
    esac
done

# The release is a statement about one commit of main: the payload stamp
# inside every installer (build_common.stage_payload) is what docs/AUDIT.md
# compares against the tagged tree, so the tree that built them must be it.
branch=$(git -C "$root" rev-parse --abbrev-ref HEAD)
dirty=$(git -C "$root" status --porcelain --untracked-files=no)
if [ "$branch" != main ] || [ -n "$dirty" ]; then
    state="on $branch"; [ -z "$dirty" ] || state="$state, uncommitted changes"
    if [ $dry_run -eq 1 ]; then
        echo "dry run: the tree is not clean on main ($state) — a real run stops here"
    else
        echo "the tree must be clean and on main ($state)" >&2; exit 1
    fi
fi
commit=$(git -C "$root" rev-parse --short=12 HEAD)
tag="v$version"
download="https://github.com/$repo/releases/download/$tag"

mkdir -p "$out"
sums="$out/SHA256SUMS.txt"
: > "$sums"
for file in "${artefacts[@]}"; do
    (cd "$(dirname "$file")" && sha256sum "$(basename "$file")") >> "$sums"
done

python3 - "$repo" "$version" "$tag" "$commit" "$download" "$sums" "$out/downloads.json" \
        "${artefacts[@]}" <<'EOF'
import datetime, json, os, sys

repo, version, tag, commit, download, sums, target, *files = sys.argv[1:]
digests = {name: sha for sha, name in (line.split() for line in open(sums))}
prefix = f"Sautium-{version}-"


def asset(path):
    name = os.path.basename(path)
    stem = name[len(prefix):].rsplit(".", 1)[0]
    if name.endswith(".exe"):
        os_name, arch = "windows", "x64"          # sautium.iss: ArchitecturesAllowed=x64compatible
    else:
        os_name, arch = "macos", {"arm64": "arm64", "x86_64": "x64"}[stem]
    return {"os": os_name, "arch": arch, "name": name, "url": f"{download}/{name}",
            "sha256": digests[name], "size": os.path.getsize(path)}


document = {
    "version": version,
    "tag": tag,
    "commit": commit,
    "published_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "notes_url": f"https://github.com/{repo}/releases/tag/{tag}",
    "signed": False,
    "assets": [asset(path) for path in files],
}
with open(target, "w", encoding="utf-8") as handle:
    json.dump(document, handle, indent=2)
    handle.write("\n")
EOF
echo "wrote $sums and $out/downloads.json"
if [ $dry_run -eq 1 ]; then
    cat "$out/downloads.json"
    exit 0
fi

token=${GITHUB_TOKEN:-$(printf 'protocol=https\nhost=github.com\n\n' \
    | git credential fill | sed -n 's/^password=//p')}
[ -n "$token" ] || { echo "no GitHub token (GITHUB_TOKEN or git credential fill)" >&2; exit 1; }
api() {
    curl -sSf -H "Authorization: Bearer $token" -H "Accept: application/vnd.github+json" \
         -H "X-GitHub-Api-Version: 2022-11-28" "$@"
}
field() { python3 -c 'import json, sys; d = json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }

default_notes() {
    cat <<EOF
Sautium $version — public beta.

The Windows installer is unsigned and the macOS build is ad-hoc signed, so both systems warn on first launch:

- **Windows** — SmartScreen says the publisher is unknown: choose **More info → Run anyway**. The install is per-user, without an administrator prompt.
- **macOS** — open the DMG, drag Sautium onto Applications. The first launch is blocked: open it once, then System Settings → Privacy & Security → **Open Anyway** (or run \`xattr -dr com.apple.quarantine /Applications/Sautium.app\` first).

Check a download against \`SHA256SUMS.txt\` (\`sha256sum -c SHA256SUMS.txt --ignore-missing\` beside the file). The installer is a carrier: the first launch clones \`main\` and the app updates itself from there. How to verify what the tree does, and that this payload was built from it: [docs/AUDIT.md](https://github.com/$repo/blob/$tag/docs/AUDIT.md).

Downloads and guides: https://sautium.net/download · Built from \`$commit\`.
EOF
}

release=$(api "https://api.github.com/repos/$repo/releases/tags/$tag" 2>/dev/null || true)
if [ -z "$release" ]; then
    if [ -n "$notes" ]; then body=$(cat "$notes"); else body=$(default_notes); fi
    release=$(api -X POST "https://api.github.com/repos/$repo/releases" -d "$(python3 -c '
import json, sys
print(json.dumps({
    "tag_name": sys.argv[1], "target_commitish": "main", "name": "Sautium " + sys.argv[2],
    "body": sys.stdin.read(), "prerelease": False, "make_latest": "true",
}))' "$tag" "$version" <<<"$body")")
    echo "created release $tag"
fi
rid=$(field 'd["id"]' <<<"$release")

upload() {
    local file=$1 type=$2 name aid
    name=$(basename "$file")
    aid=$(field 'next((a["id"] for a in d.get("assets", []) if a["name"] == "'"$name"'"), "")' <<<"$release")
    if [ -n "$aid" ]; then
        api -X DELETE "https://api.github.com/repos/$repo/releases/assets/$aid"
        echo "replaced the previous $name"
    fi
    api -X POST -H "Content-Type: $type" --data-binary @"$file" \
        "https://uploads.github.com/repos/$repo/releases/$rid/assets?name=$name" >/dev/null
    echo "uploaded $name"
}
for file in "${artefacts[@]}"; do
    case "$file" in
        *.exe) upload "$file" application/vnd.microsoft.portable-executable ;;
        *.dmg) upload "$file" application/x-apple-diskimage ;;
    esac
done
upload "$sums" text/plain
upload "$out/downloads.json" application/json

check=$(mktemp -d)
trap 'rm -rf "$check"' EXIT
for file in "${artefacts[@]}" "$sums" "$out/downloads.json"; do
    name=$(basename "$file")
    # A fresh asset can answer 404 for a moment after the upload.
    curl -sSfL --retry 5 --retry-delay 3 --retry-all-errors -o "$check/$name" "$download/$name"
done
(cd "$check" && sha256sum -c --quiet SHA256SUMS.txt)
cmp -s "$sums" "$check/SHA256SUMS.txt" && cmp -s "$out/downloads.json" "$check/downloads.json" \
    || { echo "the published SHA256SUMS.txt / downloads.json differ from the local files" >&2; exit 1; }
echo "published $tag ($(wc -l < "$sums") installers, commit $commit): $download/downloads.json"
