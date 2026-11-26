#!/usr/bin/env bash
set -euo pipefail

fail() { echo "Reelucid: $*" >&2; exit 1; }

# The file uses Docker --env-file syntax. Read deployment keys without executing it.
setting() {
    local value
    value="$(sed -n "s/^$1=//p" "$env_file" | tail -n 1 | tr -d '\r')"
    printf '%s\n' "${value:-$2}"
}

migrate_artifacts() {
    local src=/state/artifacts target=/state/artifacts
    local -a mounts=(--mount "type=volume,src=$volume,dst=/state")
    if [[ -n $old_artifacts ]]; then
        mounts+=(--mount "type=bind,src=$old_artifacts,dst=/old-artifacts,readonly")
        src=/old-artifacts
    fi
    if [[ -n $artifacts ]]; then
        local parent
        parent="$(dirname -- "$artifacts")"
        mkdir -p -- "$parent"
        mounts[1]+=",readonly"
        mounts+=(--mount "type=bind,src=$parent,dst=/new-parent")
        target="/new-parent/$(basename -- "$artifacts")"
    fi

    docker run --rm -i --entrypoint /bin/sh --user 0:0 --network none --read-only \
        --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
        --security-opt no-new-privileges:true "${mounts[@]}" "$image" -eu -s -- "$src" "$target" <<'MIGRATE'
source=$1
target=$2
if [ -e "$target" ] && [ ! -d "$target" ]; then
    echo "Artifact destination is not a directory." >&2
    exit 1
fi
if [ -d "$target" ] && [ -n "$(find "$target" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    if [ -d "$source" ] && diff -qr --no-dereference -- "$source" "$target" >/dev/null; then
        exit 0
    fi
    echo "Artifact destination contains different files. Choose an empty directory." >&2
    exit 1
fi

# Stage and verify on the destination filesystem, then publish the directory in one rename.
stage="$(mktemp -d "$(dirname -- "$target")/.reelucid-artifacts.XXXXXXXX")"
trap 'rmdir -- "$stage" 2>/dev/null || rm -rf -- "$stage"' EXIT
if [ -d "$source" ]; then
    cp -a -- "$source/." "$stage/"
    diff -qr --no-dereference -- "$source" "$stage"
else
    chmod 0755 "$stage"
fi
chown 10001:10001 "$stage"
chmod u+rwx "$stage"
mv -T -- "$stage" "$target"
trap - EXIT
MIGRATE
}

rollback() {
    local status=$?
    if (( status == 0 )); then return; fi
    if [[ -n $candidate ]]; then docker rm -f "$candidate" >/dev/null || true; fi
    if (( renamed )); then docker rename "$old" "$name" || true; fi
    if [[ $running == true ]]; then docker start "$old" >/dev/null || true; fi
    echo "Reelucid deployment failed. Check docker ps -a and the messages above." >&2
}

case "${1:-}" in
    ""|--pull-only) ;;
    *) fail "Usage: bash deploy.sh [--pull-only]" ;;
esac
directory="$(cd -- "$(dirname -- "$0")" && pwd)"
env_file="$directory/.env"
[[ -r $env_file ]] || fail "Create $env_file using the example configuration."
image="$(setting REELUCID_IMAGE yuanqimanong/reelucid:1.1.5)"
docker pull "$image"
if [[ ${1:-} == --pull-only ]]; then exit 0; fi

exec 9>"$directory/.deploy.lock"
flock -n 9 || fail "Another deployment in $directory is in progress."
name="$(setting REELUCID_CONTAINER_NAME reelucid)"
volume="$(setting REELUCID_DATA_VOLUME reelucid-data)"
listen="$(setting REELUCID_LISTEN 127.0.0.1:8000)"
cpu="$(setting REELUCID_CPU_LIMIT 4.0)"
memory="$(setting REELUCID_MEMORY_LIMIT 6g)"
network="$(setting REELUCID_NETWORK '')"
artifacts="$(setting REELUCID_ARTIFACTS_DIR '')"
if [[ -n $artifacts ]]; then
    [[ $artifacts == /* && $artifacts != *,* ]] || fail "Set REELUCID_ARTIFACTS_DIR to an absolute directory path without commas."
    artifacts="$(realpath -m -- "$artifacts")"
    [[ $artifacts != / ]] || fail "Choose a dedicated artifacts directory."
fi
if [[ -n $network ]]; then docker network inspect --format '{{.Name}}' "$network" >/dev/null; fi

old="" old_artifacts="" running=false candidate="" renamed=0
for reserved in "$name-candidate" "$name-previous"; do
    if docker container inspect "$reserved" >/dev/null 2>&1; then
        fail "Container $reserved already exists. Resolve the interrupted deployment first."
    fi
done
if old="$(docker container inspect --format '{{.Id}}' "$name" 2>/dev/null)"; then
    old_volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}' "$old")"
    [[ $old_volume == "$volume" ]] || fail "Set REELUCID_DATA_VOLUME to the existing /data volume."
    old_mount="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/data/artifacts"}}{{.Type}}={{.Source}}{{end}}{{end}}' "$old")"
    case "$old_mount" in
        bind=*) old_artifacts="$(realpath -m -- "${old_mount#bind=}")" ;;
        "") ;;
        *) fail "Migrate the existing non-bind artifact mount before using this script." ;;
    esac
    running="$(docker inspect --format '{{.State.Running}}' "$old")"
fi
docker volume create "$volume" >/dev/null
if [[ $artifacts != "$old_artifacts" ]]; then
    volume_artifacts="$(docker volume inspect --format '{{.Mountpoint}}/artifacts' "$volume")"
    before="${old_artifacts:-$volume_artifacts}" after="${artifacts:-$volume_artifacts}"
    if [[ $before == "$after/"* || $after == "$before/"* || $before == "$after" ]]; then
        fail "Choose a directory separate from the current artifact storage."
    fi
fi

options=(--env-file "$env_file" --env REELUCID_DATA_DIR=/data
    --publish "$listen:8000" --mount "type=volume,src=$volume,dst=/data"
    --cpus "$cpu" --memory "$memory" --restart unless-stopped --stop-timeout 45
    --read-only --cap-drop ALL --security-opt no-new-privileges:true
    --tmpfs /tmp:rw,size=64m,mode=1777)
if [[ -n $artifacts ]]; then options+=(--mount "type=bind,src=$artifacts,dst=/data/artifacts"); fi

if [[ $running == true ]]; then
    active="$(docker exec -i "$old" python - <<'PY'
import sqlite3
with sqlite3.connect("file:/data/db/reelucid.db?mode=ro", uri=True) as db:
    print(db.execute("SELECT count(*) FROM jobs WHERE NOT deleted AND status IN ('queued', 'running', 'cancelling')").fetchone()[0])
PY
)"
    [[ $active == 0 ]] || fail "Wait for $active active jobs before deploying."
fi
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ $running == true ]]; then docker stop "$old" >/dev/null; fi
if [[ $artifacts != "$old_artifacts" ]]; then migrate_artifacts; fi
candidate="$(docker create --name "$name-candidate" "${options[@]}" "$image")"
if [[ -n $old ]]; then
    docker rename "$old" "$name-previous"
    renamed=1
fi
docker rename "$candidate" "$name"
docker start "$candidate" >/dev/null
for ((attempt=0; attempt<90; attempt++)); do
    if docker exec "$candidate" reelucid healthcheck >/dev/null 2>&1; then break; fi
    sleep 1
done
(( attempt < 90 )) || fail "The new container did not become ready."
docker exec "$candidate" python -c '
from tempfile import TemporaryDirectory
with TemporaryDirectory(dir="/data/artifacts"):
    pass
'
if [[ -n $network ]]; then docker network connect --alias "$name" "$network" "$candidate"; fi
if [[ -n $old ]]; then docker rm "$old" >/dev/null; fi
trap - EXIT INT TERM
docker inspect --format '{{.Name}} {{.Config.Image}} {{.Image}}' "$candidate"
