#!/usr/bin/env bash
# release-public.sh — build a fresh one-commit repo from a ref, scan it, and
# (only when asked) push it.
#
# The public repo never gets this repo's history: each release is the tree at
# <ref>, committed once, after two scans come out clean:
#   - gitleaks (secrets), run from its container image
#   - scripts/release_check.py against a deny list kept outside the repo
#
# Usage:
#   scripts/release-public.sh --deny <denylist> [--ref origin/main] [--out DIR]
#                             [--message "span-viewer 1.0.0"] [--push <remote url> [--branch main]]
#
# Without --push it stops after the scans and prints where the tree is.
# The commit's author is RELEASE_AUTHOR ("Name <email>"), else your git
# config; the deny list checks it too, so a private address can't ride along.
# Needs git, docker (for gitleaks) and python3.
set -euo pipefail

REF=origin/main
OUT=
DENY=
MSG=
PUSH=
BRANCH=main
GITLEAKS_IMAGE=${GITLEAKS_IMAGE:-zricethezav/gitleaks:v8.21.2}
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --deny) DENY=$2; shift 2 ;;
    --message) MSG=$2; shift 2 ;;
    --push) PUSH=$2; shift 2 ;;
    --branch) BRANCH=$2; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "release-public: unknown argument $1" >&2; exit 2 ;;
  esac
done
[ -n "$DENY" ] && [ -f "$DENY" ] || { echo "release-public: --deny <file> is required" >&2; exit 2; }
DENY=$(cd "$(dirname "$DENY")" && pwd)/$(basename "$DENY")

SRC=$(git rev-parse --show-toplevel)
SHA=$(git -C "$SRC" rev-parse --verify "$REF^{commit}")
OUT=${OUT:-$(mktemp -d "${TMPDIR:-/tmp}/span-viewer-release.XXXXXX")}
[ -z "$(ls -A "$OUT" 2>/dev/null)" ] || { echo "release-public: $OUT is not empty" >&2; exit 2; }
MSG=${MSG:-"span-viewer $(date -u +%Y-%m-%d)"}

echo "== tree of $REF ($SHA) into $OUT"
mkdir -p "$OUT"
git -C "$SRC" archive --format=tar "$SHA" | tar -C "$OUT" -xf -

echo "== one commit"
git -C "$OUT" init -q -b "$BRANCH"
git -C "$OUT" add -A
AUTHOR=()
[ -z "${RELEASE_AUTHOR:-}" ] || AUTHOR=(--author="$RELEASE_AUTHOR")
if [ -n "${RELEASE_AUTHOR:-}" ]; then
  NAME=${RELEASE_AUTHOR%% <*}; EMAIL=${RELEASE_AUTHOR##*<}; EMAIL=${EMAIL%>}
  export GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL"
fi
git -C "$OUT" -c core.autocrlf=false commit -q "${AUTHOR[@]}" -m "$MSG"
git -C "$OUT" log --format='%h %an <%ae> / %cn <%ce>: %s'

echo "== gitleaks"
docker run --rm -v "$OUT":/repo "$GITLEAKS_IMAGE" git /repo --redact --no-banner

echo "== deny list"
python3 "$SRC/scripts/release_check.py" --deny "$DENY" "$OUT"
META=$(mktemp -d)
git -C "$OUT" log --format='%an <%ae>%n%cn <%ce>%n%B' > "$META/commit.txt"
python3 "$SRC/scripts/release_check.py" --deny "$DENY" "$META"
rm -rf "$META"

if [ -z "$PUSH" ]; then
  echo "release-public: clean. Not pushed; the tree is in $OUT"
  echo "  push it with: git -C $OUT push <remote url> $BRANCH"
  exit 0
fi
echo "== push to $PUSH ($BRANCH)"
git -C "$OUT" push "$PUSH" "$BRANCH:$BRANCH"
