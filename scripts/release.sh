#!/usr/bin/env bash
# Create a GitHub release for the version in the root stub and upload the four bundles.
#   scripts/release.sh            create the release
#   DRY_RUN=1 scripts/release.sh  run every check, print the gh command, change nothing
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

ENV_NAME="${MAMBA_ENV:-webui-dev}"
if [ "${CONDA_DEFAULT_ENV:-}" != "$ENV_NAME" ]; then
  exec micromamba run -n "$ENV_NAME" bash "${BASH_SOURCE[0]}" "$@"
fi

BUNDLES=(open_webui_openrouter_pipe_bundled.py
         open_webui_openrouter_pipe_bundled_compressed.py
         open_webui_openrouter_pipe_bundled_no_plugins.py
         open_webui_openrouter_pipe_bundled_compressed_no_plugins.py)
NOTES_FILE=RELEASE_NOTES_PENDING.md
die() { echo "release: $*" >&2; exit 1; }

command -v gh >/dev/null || die "the GitHub CLI 'gh' is not installed"
[ "${DRY_RUN:-}" = 1 ] || gh auth status >/dev/null 2>&1 || die "gh is not logged in (run: gh auth login)"

version=$(sed -n 's/^version: *//p' open_webui_openrouter_pipe.py | head -1)
[ -n "$version" ] || die "no 'version:' line in open_webui_openrouter_pipe.py"
py_version=$(sed -n 's/^version *= *"\(.*\)"/\1/p' pyproject.toml | head -1)
[ "$version" = "$py_version" ] || die "version mismatch: stub $version, pyproject.toml $py_version"
tag="v$version"

git diff --quiet HEAD -- || die "uncommitted changes to tracked files; commit them first"
branch=$(git rev-parse --abbrev-ref HEAD)
git fetch -q origin "$branch" || die "could not fetch origin/$branch"
[ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$branch")" ] \
  || die "HEAD is not the same as origin/$branch; push (or pull) first so the tag lands on a published commit"

if gh release view "$tag" >/dev/null 2>&1; then
  die "release $tag already exists; bump 'version:' in open_webui_openrouter_pipe.py and pyproject.toml"
fi

make all
for f in "${BUNDLES[@]}"; do [ -s "$f" ] || die "missing bundle $f"; done

args=(release create "$tag" "${BUNDLES[@]}" --target "$(git rev-parse HEAD)" --title "$tag")
BODY_LIMIT=120000
body_file=""
if [ -s "$NOTES_FILE" ]; then
  if [ "$(wc -m < "$NOTES_FILE")" -le "$BODY_LIMIT" ]; then
    args+=(--notes-file "$NOTES_FILE")
  else
    body_file=$(mktemp)
    trap 'rm -f "$body_file"' EXIT
    python - "$NOTES_FILE" "$body_file" "$BODY_LIMIT" <<'PY'
import sys
src, dst, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
cut = open(src, encoding="utf-8").read()[:limit].rsplit("\n", 1)[0]
note = "\n\n---\n\n_These notes exceed GitHub's release body limit and are cut here. The full text is attached as `RELEASE_NOTES.md`._\n"
open(dst, "w", encoding="utf-8").write(cut + note)
PY
    full_notes="$(dirname "$body_file")/RELEASE_NOTES.md"
    cp "$NOTES_FILE" "$full_notes"
    trap 'rm -f "$body_file" "$full_notes"' EXIT
    echo "release: $NOTES_FILE is over $BODY_LIMIT characters; truncating the body and attaching the full file"
    args+=("$full_notes" --notes-file "$body_file")
  fi
else
  args+=(--generate-notes)
fi

echo "release: $tag from $(git rev-parse --short HEAD) on $branch"
if [ "${DRY_RUN:-}" = 1 ]; then
  echo "release: dry run, would run: gh ${args[*]}"
  exit 0
fi
gh "${args[@]}"
