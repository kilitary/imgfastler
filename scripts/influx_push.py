#!/usr/bin/env python3
"""Snapshot Docker state into InfluxDB as line protocol.

Collects, per run:
  docker_container  — every container: name, image, state, networks, ports
  docker_image      — every image: repo, tag, id, size
  docker_network    — every network: name, driver, subnet, attached containers
  docker_connection — TCP/UDP sockets of running containers (via the
                      imgfastler backend's /connections endpoint, which reads
                      each container's network namespace)
  docker_stats      — one-shot CPU/memory/network/block-IO stats

Stdlib only. Uses the `docker` CLI for listings/stats and the imgfastler
backend (http://127.0.0.1:8099) for connections.

Usage:
    python3 scripts/influx_push.py            # single snapshot
    python3 scripts/influx_push.py --loop 30  # snapshot every N seconds
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

INFLUX_URL = "http://127.0.0.1:8086"
TOKEN = "imgfastler-telemetry-token"
ORG = "imgfastler"
BUCKET = "docker"
BACKEND_URL = "http://127.0.0.1:8099"


def esc(value) -> str:
    """Escape a tag value."""
    return str(value).replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def esc_field_str(value) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_json(args: list[str]) -> list[dict]:
    out = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {out.stderr.strip()}")
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def _container_lines() -> list[str]:
    lines = []
    for c in run_json(["docker", "ps", "-a", "--format", "{{json .}}"]):
        name = c["Names"]
        nets_out = subprocess.run(
            ["docker", "inspect", c["ID"], "--format", "{{json .NetworkSettings.Networks}}"],
            capture_output=True, text=True, timeout=30,
        )
        try:
            nets = list(json.loads(nets_out.stdout or "{}").keys()) if nets_out.returncode == 0 else []
        except json.JSONDecodeError:
            nets = []
        tags = (
            f"name={esc(name)},image={esc(c['Image'])},state={esc(c['State'])}"
        )
        size_match = re.match(r"(\S+)\s+\(virtual\s+(\S+)\)", c["Size"] or "")
        if size_match:
            size, virtual = _bytes(size_match.group(1)), _bytes(size_match.group(2))
        else:
            size, virtual = _bytes(c["Size"] or ""), 0
        fields = (
            f"status={esc_field_str(c['Status'])},"
            f"size_bytes={size}i,"
            f"virtual_bytes={virtual}i,"
            f"networks={esc_field_str(','.join(nets))}"
        )
        lines.append(f"docker_container,{tags} {fields}")
    return lines


def _image_lines() -> list[str]:
    lines = []
    for img in run_json(["docker", "images", "--format", "{{json .}}"]):
        repo, tag = img["Repository"], img["Tag"]
        if not tag:
            continue
        ref = f"{repo}:{tag}" if tag != "<none>" else repo
        tags = f"repo={esc(repo)},tag={esc(tag)},ref={esc(ref)}"
        fields = f"size_bytes={_bytes(img['Size'])}i,created_at={esc_field_str(img['CreatedAt'])}"
        lines.append(f"docker_image,{tags} {fields}")
    return lines


def _network_lines() -> list[str]:
    lines = []
    nets = run_json(["docker", "network", "ls", "--format", "{{json .}}"])
    for net in nets:
        inspect = subprocess.run(
            ["docker", "network", "inspect", net["Name"], "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=30,
        )
        subnet = ""
        container_count = 0
        try:
            attrs = json.loads(inspect.stdout)
            if isinstance(attrs, list):
                attrs = attrs[0]
            ipam = (attrs.get("IPAM") or {}).get("Config") or []
            if ipam:
                subnet = ipam[0].get("Subnet") or ""
            container_count = len(attrs.get("Containers") or {})
        except (ValueError, KeyError, IndexError):
            pass
        tags = f"name={esc(net['Name'])},driver={esc(net['Driver'])}"
        fields = f"containers={container_count}i,subnet={esc_field_str(subnet)}"
        lines.append(f"docker_network,{tags} {fields}")
    return lines


def _connections_lines() -> list[str]:
    lines = []
    try:
        containers = json.loads(
            urllib.request.urlopen(f"{BACKEND_URL}/api/containers", timeout=10).read()
        )
    except (urllib.error.URLError, ValueError) as exc:
        print(f"note: backend unavailable, skipping connections: {exc}", file=sys.stderr)
        return lines
    containers = [
        c
        for c in containers
        if c.get("state") in ("running", "paused")
    ]

    def fetch(container: dict) -> tuple[dict, dict | None]:
        url = (
            f"{BACKEND_URL}/api/containers/{urllib.parse.quote(container['id'])}/connections"
        )
        try:
            return container, json.loads(urllib.request.urlopen(url, timeout=15).read())
        except (urllib.error.URLError, ValueError):
            return container, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        for container, data in pool.map(fetch, containers):
            if data is None:
                continue
            counts: dict[tuple[str, str], int] = {}
            for conn in data.get("connections") or []:
                tags = (
                    f"container={esc(container['name'])},"
                    f"protocol={esc(conn['protocol'])},"
                    f"state={esc(conn['state'])},"
                    f"family={esc(conn['family'])}"
                )
                endpoint = (
                    f"{conn['local_address']}:{conn['local_port'] or '*'}"
                    f" <-> {conn['remote_address']}:{conn['remote_port'] or '*'}"
                )
                fields = (
                    f"local_address={esc_field_str(conn['local_address'])},"
                    f"local_port={conn['local_port'] or 0}i,"
                    f"remote_address={esc_field_str(conn['remote_address'])},"
                    f"remote_port={conn['remote_port'] or 0}i,"
                    f"inode={conn['inode'] or 0}i,"
                    f"endpoint={esc_field_str(endpoint)}"
                )
                lines.append(f"docker_connection,{tags} {fields}")
                counts[(container["name"], conn["state"])] = (
                    counts.get((container["name"], conn["state"]), 0) + 1
                )
            for (name, state), n in sorted(counts.items()):
                lines.append(
                    f"docker_conn_count,container={esc(name)},state={esc(state)} connections={n}i"
                )
    return lines


def _stats_lines() -> list[str]:
    lines = []
    for s in run_json(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}"]
    ):
        name = s["Name"]
        cpu = float((s["CPUPerc"] or "0").rstrip("%") or 0)
        memp = float((s["MemPerc"] or "0").rstrip("%") or 0)
        fields = (
            f"cpu_percent={cpu},"
            f"mem_usage={_bytes(s['MemUsage'].split('/')[0])}i,"
            f"mem_limit={_bytes(s['MemUsage'].split('/')[1])}i,"
            f"mem_percent={memp},"
            f"net_rx={_bytes(s['NetIO'].split('/')[0])}i,"
            f"net_tx={_bytes(s['NetIO'].split('/')[1])}i,"
            f"block_read={_bytes(s['BlockIO'].split('/')[0])}i,"
            f"block_write={_bytes(s['BlockIO'].split('/')[1])}i,"
            f"pids={s['PIDs'] or 0}i"
        )
        lines.append(f"docker_stats,container={esc(name)} {fields}")
    return lines


def _bytes(value: str) -> int:
    value = (value or "0").strip()
    if value in ("--", "-", ""):
        return 0
    multipliers = {"B": 1, "kB": 1e3, "KB": 1e3, "KiB": 1024, "MB": 1e6, "MiB": 1024**2,
                   "GB": 1e9, "GiB": 1024**3, "TB": 1e12, "TiB": 1024**4}
    for suffix, mult in sorted(multipliers.items(), key=lambda kv: -len(kv[0])):
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)].strip()) * mult)
    try:
        return int(float(value))
    except ValueError:
        return 0


def snapshot() -> int:
    lines = _container_lines() + _image_lines() + _network_lines() + _stats_lines() + _connections_lines()
    payload = "\n".join(lines) + "\n"
    query = urllib.parse.urlencode({"org": ORG, "bucket": BUCKET, "precision": "s"})
    request = urllib.request.Request(
        f"{INFLUX_URL}/api/v2/write?{query}",
        data=payload.encode(),
        headers={
            "Authorization": f"Token {TOKEN}",
            "Content-Type": "text/plain; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        print(f"wrote {len(lines)} points ({response.status})")
    return len(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                        help="keep snapshotting every SECONDS instead of once")
    args = parser.parse_args()
    if args.loop:
        while True:
            try:
                snapshot()
            except Exception as exc:
                print(f"snapshot failed: {exc}", file=sys.stderr)
            time.sleep(args.loop)
    else:
        snapshot()


if __name__ == "__main__":
    main()
