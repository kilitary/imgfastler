#!/usr/bin/bash
# Build the InfluxDB collector cron image (imgfastler-collector:latest).
#
# The image packages scripts/influx_push.py on alpine with busybox crond and
# collects Docker state into InfluxDB every 10 seconds (6 cron firings per
# minute spaced with `sleep 10`, deduplicated with flock).
#
# Usage:
#   scripts/build_collector_image.sh [tag]     # default tag: imgfastler-collector:latest
#
# Then run it (see `make run-collector`):
#   docker run -d --name collector-stack --network host \
#     --restart unless-stopped \
#     -v /var/run/docker.sock.raw:/var/run/docker.sock \
#     imgfastler-collector:latest
set -euo pipefail

TAG=${1:-imgfastler-collector:latest}
ROOT=$(cd "$(dirname "$0")/.." && pwd)

CONTEXT=$(mktemp -d)
trap 'rm -rf "$CONTEXT"' EXIT

# Minimal build context: just the collector needs, keeps the daemon from
# shipping the whole repo (incl. .venv) to itself.
cp "$ROOT/scripts/influx_push.py" "$CONTEXT/"
cp "$ROOT/collector/Dockerfile" "$ROOT/collector/entrypoint.sh" "$CONTEXT/"

docker build --tag="$TAG" "$CONTEXT"
echo
echo "Built $TAG."
echo "Create the container with: make run-collector"
