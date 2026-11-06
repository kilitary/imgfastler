#!/usr/bin/env python3
"""End-to-end smoke test for the imgfastler backend.

Starts nothing by itself: point it at a running backend.

    make run-backend          # terminal 1, serves on 127.0.0.1:8099
    make test-backend         # terminal 2, this script

It walks through the same flow the UI performs: list images/networks, create a
container with environment variables, a port mapping, a bind mount, a named
volume and two networks, then start/stop/remove it again.

Uses the Python standard library only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("API_BASE", "http://127.0.0.1:8099")

CONTAINER_NAME = "imgfastler-smoke"
NETWORK_NAME = "imgfastler-smoke-net"
VOLUME_NAME = "imgfastler-smoke-vol"

failures: list[str] = []


def step(message: str) -> None:
    print(f"  - {message}", flush=True)


def request(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as response:
        payload = response.read().decode()
        return json.loads(payload) if payload else None


def expect(condition: bool, message: str) -> bool:
    if condition:
        step(message)
        return True
    failures.append(message)
    print(f"  ! FAILED: {message}", flush=True)
    return False


def docker_cli(*args: str) -> bool:
    """Helper for setup/teardown that the API does not expose."""
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", *args], capture_output=True, check=False).returncode == 0


def pick_image() -> str:
    images = request("GET", "/api/images")
    tagged = [image for image in images if image["tags"]]
    if not tagged:
        sys.exit("No tagged image available locally - pull one first (e.g. docker pull alpine).")
    for preferred in ("alpine", "nginx", "busybox", "ubuntu"):
        for image in tagged:
            if image["tags"][0].split(":")[0].endswith(preferred):
                return image["tags"][0]
    return tagged[0]["tags"][0]


def main() -> int:
    print(f"Smoke testing {BASE}\n")

    health = request("GET", "/api/health")
    engine = health.get("engine") or {}
    expect(health.get("status") == "ok", f"health ok (engine {engine.get('version')})")

    image = pick_image()
    step(f"using image {image}")

    images = request("GET", "/api/images")
    expect(len(images) > 0, f"{len(images)} image(s) listed")

    networks = request("GET", "/api/networks")
    names = [network["name"] for network in networks]
    expect("bridge" in names, "default bridge network listed")

    created_network = docker_cli("network", "create", NETWORK_NAME)
    expected_networks = names[:2]
    if created_network:
        expected_networks = ["bridge", NETWORK_NAME]
        step(f"created helper network {NETWORK_NAME}")

    # The command is optional: fall back to the image default if it cannot run sleep.
    spec = {
        "image": image,
        "name": CONTAINER_NAME,
        "command": "sleep 3600",
        "environment": {"IMGFASTER_SMOKE": "1", "TZ": "UTC"},
        "ports": [{"host_port": 18973, "container_port": 80, "host_ip": "", "protocol": "tcp"}],
        # Bind source must be shared from the host (Docker Desktop file-sharing
        # whitelist); /tmp is not always whitelisted, /etc typically is.
        "mounts": [
            {"type": "bind", "source": "/etc", "target": "/mnt/tmp-smoke", "read_only": True},
            {"type": "volume", "source": VOLUME_NAME, "target": "/mnt/data"},
        ],
        "networks": expected_networks,
        "restart_policy": "no",
    }

    created = None
    try:
        try:
            created = request("POST", "/api/containers", spec)
            step(f"container created: {created['name']} ({created['short_id']})")
        except urllib.error.HTTPError as error:
            if "sleep" not in error.read().decode().lower():
                raise
            step("image has no `sleep` binary - retrying with the image default command")
            spec["command"] = None
            created = request("POST", "/api/containers", spec)
            step(f"container created: {created['name']} ({created['short_id']})")

        expect(not created.get("warnings"), "no attach warnings")

        containers = request("GET", "/api/containers")
        match = next((c for c in containers if c["name"] == CONTAINER_NAME), None)
        expect(match is not None, "container appears in the container list")
        if match:
            expect(
                any("18973" in str(port) for port in match["ports"]),
                f"port binding present: {match['ports']}",
            )
            attached = set(match["networks"])
            expect(
                set(expected_networks) <= attached,
                f"networks attached: {sorted(attached)}",
            )
            read_only_mounts = [m for m in match["mounts"] if m.get("read_only")]
            expect(
                len(read_only_mounts) == 1,
                f"read-only bind mount reported back: {match['mounts']}",
            )

        time.sleep(1)
        state = (next((c for c in request("GET", "/api/containers") if c["name"] == CONTAINER_NAME), {}) or {}).get("state")
        if state == "running":
            expect(True, "container is running")
            request("POST", f"/api/containers/{created['id']}/stop")
            time.sleep(1)
            stopped = next(
                (c for c in request("GET", "/api/containers") if c["name"] == CONTAINER_NAME), {}
            )
            expect(stopped.get("state") == "exited", "container stopped")
            request("POST", f"/api/containers/{created['id']}/start")
            step("container started again")
        else:
            step(f"container state is '{state}' (image has no long-running command)")

        volumes = request("GET", "/api/volumes")
        expect(VOLUME_NAME in [volume["name"] for volume in volumes], "named volume was created")

        # Invalid payloads must be rejected with a helpful message.
        try:
            request("POST", "/api/containers", {"image": image, "networks": ["does-not-exist"]})
            expect(False, "unknown network rejected")
        except urllib.error.HTTPError as error:
            expect(error.code == 404, f"unknown network rejected with 404 ({error.code})")

        try:
            request("POST", "/api/containers", {"image": "imgfastler/no-such-image:latest"})
            expect(False, "missing image rejected")
        except urllib.error.HTTPError as error:
            expect(error.code == 404, f"missing image rejected with 404 ({error.code})")
    finally:
        request("DELETE", f"/api/containers/{CONTAINER_NAME}?force=true")
        step("cleaned up container")
        if created_network:
            docker_cli("network", "rm", NETWORK_NAME)
            step("cleaned up helper network")

    print()
    if failures:
        print(f"FAILED ({len(failures)} check(s)):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("All smoke checks passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.URLError as error:
        print(f"Cannot reach the backend at {BASE}: {error.reason}")
        print("Start it with `make run-backend` first.")
        sys.exit(2)
