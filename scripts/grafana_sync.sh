#!/usr/bin/env bash
# Sync repo Grafana config into the VM-shared staging area and recreate
# grafana-stack so it provisions datasources + dashboards from files.
#
# Why: /mnt/projects is not shared with the Docker Desktop VM, but the user
# home directory is — and /tmp is wiped periodically, which used to break the
# bind mounts ("bind source path does not exist: /host_mnt/tmp/opencode/...").
# The repo is the source of truth; this script copies it to the persistent
# staging dir and (re)creates the container with the right bind mounts.
#
# Usage: scripts/grafana_sync.sh [--no-recreate]
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
STAGE_PROVISIONING=/home/deconf/imgfastler/grafana-provisioning
STAGE_DASHBOARDS=/home/deconf/imgfastler/grafana-dashboards
BACKEND=http://127.0.0.1:8099
API=http://127.0.0.1:3000

mkdir -p "$STAGE_PROVISIONING/datasources" "$STAGE_PROVISIONING/dashboards" "$STAGE_DASHBOARDS"
cp "$ROOT/grafana/provisioning/datasources/"* "$STAGE_PROVISIONING/datasources/"
cp "$ROOT/grafana/provisioning/dashboards/"* "$STAGE_PROVISIONING/dashboards/"
cp "$ROOT/grafana/dashboards/"* "$STAGE_DASHBOARDS/"
echo "staged provisioning + dashboards -> $STAGE_PROVISIONING, $STAGE_DASHBOARDS"

if [[ "${1:-}" == "--no-recreate" ]]; then
  exit 0
fi

# Recreate grafana-stack (delete if it exists in any state).
CID=$(docker inspect -f '{{.Id}}' grafana-stack 2>/dev/null || true)
if [[ -n "$CID" ]]; then
  curl -s -X DELETE "$BACKEND/api/containers/$CID?force=true" >/dev/null
  echo "removed old grafana-stack"
fi

curl -s -X POST "$BACKEND/api/containers" -H 'Content-Type: application/json' -d '{
  "image": "grafana/grafana:latest",
  "name": "grafana-stack",
  "ports": [{"container_port": 3000, "host_port": 3000}],
  "restart_policy": "unless-stopped",
  "mounts": [
    {"type": "bind", "source": "'"$STAGE_PROVISIONING"'", "target": "/etc/grafana/provisioning", "read_only": true},
    {"type": "bind", "source": "'"$STAGE_DASHBOARDS"'", "target": "/var/lib/grafana/dashboards", "read_only": true}
  ],
  "networks": ["grafana-net"]
}' | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("name"), d.get("state"), d.get("detail") or "")'

echo "waiting for Grafana to come up..."
for _ in $(seq 1 30); do
  sleep 2
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "$API/api/health" || true)
  [[ "$code" == "200" ]] && break
done

# Ensure the influxdb datasource plugin is present. The image bundles it, but
# Grafana 13's in-place plugin updater has been observed deleting the bundled
# copy and then failing the re-download (registry timeouts), leaving the
# datasource unregistered ("plugin.notRegistered" -> empty panels). Install it
# explicitly on every recreate; the CLI is a no-op when it already exists.
docker exec -u root grafana-stack sh -c \
  'grafana cli --pluginsDir /var/lib/grafana/plugins plugins install influxdb' \
  >/dev/null 2>&1 || true
if docker exec grafana-stack test -d /var/lib/grafana/plugins/influxdb; then
  echo "influxdb plugin present"
  docker restart grafana-stack >/dev/null
  for _ in $(seq 1 30); do
    sleep 2
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "$API/api/health" || true)
    [[ "$code" == "200" ]] && break
  done
else
  echo "WARNING: influxdb plugin still missing" >&2
fi

curl -s -m 5 "$API/api/health" && echo

# Verify what provisioning actually loaded.
curl -s -m 10 -u admin:admin "$API/api/datasources" \
  | python3 -c 'import json,sys; [print("datasource:", d["name"], "->", d["url"]) for d in json.load(sys.stdin)]'
curl -s -m 10 -u admin:admin "$API/api/search?query=Docker" \
  | python3 -c 'import json,sys; [print("dashboard:", d["title"], d["url"]) for d in json.load(sys.stdin)]'
