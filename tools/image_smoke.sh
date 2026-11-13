#!/usr/bin/env bash
# Finite acceptance: no network, real worker processes, fixture-only inputs.
set -euo pipefail
image=${1:?Usage: tools/image_smoke.sh IMAGE}
name="sg-image-check-$$"
volume="${name}-state"
cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker volume create "$volume" >/dev/null
docker run --rm --network none --user 0:0 --entrypoint python \
  --mount "type=volume,src=$volume,dst=/state" "$image" \
  -c 'import os; os.chown("/state",10001,10001); os.chmod("/state",0o700)'
hardening=(--network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,size=256m,uid=10001,gid=10001,mode=700)
docker run -d --name "$name" "${hardening[@]}" \
  --tmpfs /var/lib/session-generator:rw,nosuid,size=1g,uid=10001,gid=10001,mode=700 "$image" >/dev/null
ready=false
for attempt in {1..45}; do
  if docker exec "$name" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=2).read()" >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then docker logs "$name"; exit 1; fi
docker exec "$name" python -m tools.container_smoke
docker stop --timeout 90 "$name" >/dev/null
# Uvicorn re-raises SIGTERM after its lifespan finishes (exit 143). Require
# completed application cleanup as well; never treat SIGKILL/137 as graceful.
exit_code=$(docker inspect --format '{{.State.ExitCode}}' "$name")
[[ "$exit_code" == 0 || "$exit_code" == 143 ]]
docker logs "$name" 2>&1 | grep -q '"event":"server_stopped"'
docker rm "$name" >/dev/null
docker run --rm "${hardening[@]}" --mount "type=volume,src=$volume,dst=/var/lib/session-generator" \
  "$image" python -m tools.cloud_smoke
docker run --rm "${hardening[@]}" --mount "type=volume,src=$volume,dst=/var/lib/session-generator" \
  "$image" python -m tools.cloud_smoke --verify-stopped
