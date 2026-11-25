#!/usr/bin/env bash
# Test an already built/published image; never push or delete deployment volumes.
set -euo pipefail
cd "$(dirname "$0")/.."
export REELUCID_IMAGE="${1:?Usage: smoke_release_image.sh IMAGE [never|always]}"
pull="${2:-never}"
case "$pull" in never|always) ;; *) echo 'Expected pull policy never or always' >&2; exit 2;; esac
version="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
python scripts/check_release_version.py
work="$(mktemp -d)"
# Unique project and volumes per invocation, even when the repo already has a deployment.
project="reelucid-smoke-$(basename "$work" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9')"
compose=(docker compose -p "$project" -f compose.yaml -f tests/compose.release-smoke.yaml)
cleanup() {
    rc=$?
    trap - EXIT
    if (( rc != 0 )); then "${compose[@]}" logs --no-color || true; fi
    "${compose[@]}" down --volumes --remove-orphans || rc=1
    rm -rf -- "$work"
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Base compose publishes 127.0.0.1:8000; run on a test host with port 8000 free.
"${compose[@]}" up -d --no-build --pull "$pull" --wait --wait-timeout 150 reelucid
image_id="$("${compose[@]}" images -q reelucid)"
[[ "$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$image_id")" == "$version" ]]
"${compose[@]}" exec -T reelucid python -c 'import sys; from importlib.metadata import version; import reelucid; assert version("reelucid") == reelucid.__version__ == sys.argv[1]' "$version"
"${compose[@]}" exec -T reelucid ffmpeg -v error -y \
    -f lavfi -i 'color=c=black:s=160x120:r=10' \
    -f lavfi -i "flite=text='The meeting is on Monday. Please send the budget report before Friday.':voice=slt" \
    -shortest -c:v ffv1 -c:a pcm_s16le /data/work/tmp/reelucid-speech.mkv
"${compose[@]}" cp reelucid:/data/work/tmp/reelucid-speech.mkv "$work/speech.mkv"
REELUCID_TOKEN="$("${compose[@]}" exec -T reelucid cat /data/secrets/admin_token)"
if [[ "${GITHUB_ACTIONS:-}" == true ]]; then printf '::add-mask::%s\n' "$REELUCID_TOKEN"; fi
export REELUCID_TOKEN REELUCID_BASE_URL=http://127.0.0.1:8000
python scripts/smoke_source_http.py create --version "$version" --state "$work/source.json" --fixture "$work/speech.mkv"
# Force a replacement container with the same named data volume; do not reseed secrets.
"${compose[@]}" up -d --no-deps --no-build --pull never --force-recreate --wait --wait-timeout 150 reelucid
python scripts/smoke_source_http.py verify --version "$version" --state "$work/source.json"
python tests/smoke_transcode_http.py "$work/speech.mkv"
