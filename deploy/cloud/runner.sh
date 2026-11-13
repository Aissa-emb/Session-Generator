#!/usr/bin/env bash
# No shell-sourced environment files: Compose parses only its own env-file format.
set -euo pipefail
root_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
env_file=/srv/session-generator/.env
if [[ "${1:-}" == --env-file ]]; then
  [[ $# -ge 3 ]] || { echo 'Usage: runner.sh [--env-file PATH] COMMAND' >&2; exit 2; }
  env_file=$2
  shift 2
fi
command_name=${1:-help}
if [[ $# -gt 1 ]]; then echo 'Unexpected arguments.' >&2; exit 2; fi
case "$command_name" in
  check|start|update|status|pause|stop|resume|logs|down) ;;
  *) echo 'Usage: runner.sh [--env-file PATH] {check|start|update|status|pause|stop|resume|logs|down}'
     echo 'start/update authorize continuous synthetic traffic and optional source-map uploads.'
     [[ "$command_name" == help ]] && exit 0 || exit 2 ;;
esac
[[ -f "$env_file" ]] || { echo "Configuration missing: $env_file. Run deploy/cloud/setup.py first." >&2; exit 2; }
compose=(docker compose --project-name session-generator-cloud --env-file "$env_file" -f "$root_dir/docker-compose.cloud.yml")
check() { "${compose[@]}" --profile check run --rm --no-deps check; }
case "$command_name" in
  check) check ;;
  start) check; "${compose[@]}" up -d --no-deps generator ;;
  update) check; "${compose[@]}" stop generator; "${compose[@]}" up -d --no-deps generator ;;
  status|pause|stop|resume) "${compose[@]}" exec -T generator python -m server.cloud "$command_name" ;;
  logs) "${compose[@]}" logs --tail 100 -f generator ;;
  down) "${compose[@]}" down ;;
esac
