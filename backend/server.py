"""imgfastler extension backend.

A small REST API that talks to the Docker Engine and is served over a Unix
domain socket. Docker Desktop proxies the calls made by the extension UI
(``ddClient.extension.vm.service.*``) to that socket, as declared in
``metadata.json`` (``vm.exposes.socket`` / ``ui.dashboard-tab.backend.socket``).

Endpoints
---------
    GET    /api/health                     backend + engine status
    GET    /api/images                     list local images
    DELETE /api/images/{name}              remove an image
    GET    /api/networks                   list networks
    GET    /api/volumes                    list volume names
    GET    /api/containers                 list containers
    POST   /api/containers                 create + start a container
    GET    /api/containers/{id}/connections live TCP/UDP sockets of a container
    POST   /api/containers/{id}/start      start a container
    POST   /api/containers/{id}/stop       stop a container
    DELETE /api/containers/{id}            remove a container

Set ``TEST_PORT=8099`` to serve over TCP instead of the socket, which is
handy while developing the backend on its own.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import socket
import struct
import subprocess
from datetime import datetime
from typing import Any, Literal

import docker
import uvicorn
from docker.errors import APIError, ContainerError, DockerException, ImageNotFound, NotFound
from docker.types import Mount
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# --- configuration -----------------------------------------------------

DEFAULT_SOCKET = "/run/guest-services/imgfastler.sock"
RESTART_POLICIES = ("no", "always", "on-failure", "unless-stopped")
STOP_TIMEOUT = 10

# Network-connection inspection runs a short-lived helper container joined to
# the target container's network namespace. Only `/proc/net` is read, so no
# host PID namespace or extra privileges are required.
PROC_NET_FILES = ("tcp", "tcp6", "udp", "udp6")
NET_SECTION_PREFIX = "__IMFASTLER_"
HELPER_SCRIPT = (
    "for f in tcp tcp6 udp udp6; do "
    'echo "__IMFASTLER_${f}__"; '
    '/bin/cat "/proc/net/${f}" 2>/dev/null || true; '
    "done"
)

TCP_STATES = {
    "01": "ESTABLISHED",
    "02": "SYN_SENT",
    "03": "SYN_RECV",
    "04": "FIN_WAIT1",
    "05": "FIN_WAIT2",
    "06": "TIME_WAIT",
    "07": "CLOSE",
    "08": "CLOSE_WAIT",
    "09": "LAST_ACK",
    "0A": "LISTEN",
    "0B": "CLOSING",
}

# Unconnected UDP sockets are reported as CLOSE (07) by the kernel.
UDP_STATES = {
    "01": "ESTABLISHED",
    "07": "UNCONNECTED",
}

STATE_PRIORITY = {
    "LISTEN": 0,
    "ESTABLISHED": 1,
    "SYN_SENT": 2,
    "SYN_RECV": 3,
    "CLOSE_WAIT": 4,
    "FIN_WAIT1": 5,
    "FIN_WAIT2": 6,
    "TIME_WAIT": 7,
    "LAST_ACK": 8,
    "CLOSING": 9,
    "CLOSE": 10,
    "UNCONNECTED": 11,
}

log = logging.getLogger("imgfastler")
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

# --- app ---------------------------------------------------------------

app = FastAPI(title="imgfastler backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    # The extension UI is served by Docker Desktop, requests are proxied to
    # this socket; permissive CORS keeps local development in a browser easy.
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

_client: docker.DockerClient | None = None


def get_client() -> docker.DockerClient:
    """Return a cached engine client, re-creating it when it went away."""
    global _client
    if _client is None:
        _client = docker.from_env(timeout=30)
    return _client


# --- models ------------------------------------------------------------


class PortSpec(BaseModel):
    container_port: int = Field(..., ge=1, le=65535)
    host_port: int | None = Field(None, ge=1, le=65535)
    host_ip: str | None = None
    protocol: Literal["tcp", "udp"] = "tcp"


class MountSpec(BaseModel):
    type: Literal["bind", "volume"] = "bind"
    source: str = Field(..., min_length=1, description="Host path or volume name")
    target: str = Field(..., min_length=1, description="Path inside the container")
    read_only: bool = False


class ContainerSpec(BaseModel):
    image: str = Field(..., min_length=1)
    name: str | None = None
    command: str | None = None
    entrypoint: str | None = None
    hostname: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    ports: list[PortSpec] = Field(default_factory=list)
    mounts: list[MountSpec] = Field(default_factory=list)
    networks: list[str] = Field(default_factory=list)
    restart_policy: Literal["no", "always", "on-failure", "unless-stopped"] = "no"


# --- error helpers -----------------------------------------------------


def _extract_error_message(exc: Exception) -> str:
    """Extract a human-readable message from a Docker exception."""
    # Try common exception attributes first
    for attr in ("explanation", "message", "stderr"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # Try response body
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            body = response.json()
            if isinstance(body, dict) and body.get("message"):
                return str(body["message"])
        except Exception:
            text = getattr(response, "text", "")
            if text:
                return text.strip()

    # Fallback to string representation
    return str(exc) or exc.__class__.__name__


def as_http_error(exc: Exception) -> HTTPException:
    """Map a Docker client exception onto an HTTP status code."""
    message = _extract_error_message(exc)

    if isinstance(exc, (ImageNotFound, NotFound)):
        return HTTPException(status_code=404, detail=message)

    if isinstance(exc, APIError):
        status = 500
        response = getattr(exc, "response", None)
        if response is not None and 400 <= response.status_code < 600:
            status = response.status_code
        return HTTPException(status_code=status, detail=message)

    if isinstance(exc, DockerException):
        return HTTPException(status_code=503, detail=message)

    return HTTPException(status_code=500, detail=message)


# --- helpers -----------------------------------------------------------


def _created_timestamp(iso: str | None) -> int:
    """Convert ISO timestamp to Unix epoch seconds."""
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _parse_port_spec(container_port_proto: str) -> tuple[int, str]:
    """Parse '80/tcp' into (80, 'tcp')."""
    parts = container_port_proto.split("/")
    container_port = int(parts[0])
    protocol = parts[1] if len(parts) > 1 else "tcp"
    return container_port, protocol


def _port_list(ports: dict[str, Any]) -> list[str]:
    """Flatten ``{"80/tcp": [{"HostIp": "", "HostPort": "8080"}]}`` for display."""
    result: list[str] = []
    for container_port, bindings in (ports or {}).items():
        if not bindings:
            result.append(container_port)
            continue
        for binding in bindings:
            host_ip = binding.get("HostIp") or "0.0.0.0"
            host_port = binding.get("HostPort") or "?"
            result.append(f"{host_ip}:{host_port} -> {container_port}")
    return result


def _ports_structured(ports: dict[str, Any]) -> list[dict[str, Any]]:
    """Return structured port mappings for the edit form."""
    result: list[dict[str, Any]] = []
    for container_port_proto, bindings in (ports or {}).items():
        container_port, protocol = _parse_port_spec(container_port_proto)

        if not bindings:
            result.append(
                {
                    "container_port": container_port,
                    "protocol": protocol,
                    "host_port": None,
                    "host_ip": None,
                }
            )
            continue

        for binding in bindings:
            host_ip = binding.get("HostIp") or "0.0.0.0"
            host_port = binding.get("HostPort")
            result.append(
                {
                    "container_port": container_port,
                    "protocol": protocol,
                    "host_port": int(host_port) if host_port else None,
                    "host_ip": host_ip,
                }
            )
    return result


def _decode_proc_endpoint(value: str, ipv6: bool) -> tuple[str, int]:
    """Decode a ``HEXADDR:HEXPORT`` pair from ``/proc/net/{tcp,udp}``.

    IPv4 addresses are stored as one little-endian 32-bit word, IPv6 addresses
    as four little-endian 32-bit words (``/proc/net/tcp6``).
    """
    try:
        address_hex, port_hex = value.split(":", 1)
        port = int(port_hex, 16)
        if ipv6:
            words = [address_hex[i : i + 8] for i in range(0, 32, 8)]
            packed = b"".join(struct.pack("<I", int(word, 16)) for word in words)
            address = socket.inet_ntop(socket.AF_INET6, packed)
        else:
            packed = struct.pack("<I", int(address_hex, 16))
            address = socket.inet_ntop(socket.AF_INET, packed)
    except (OSError, ValueError):
        return value, 0
    return address, port


def _parse_proc_net(text: str) -> list[dict[str, Any]]:
    """Turn the helper container's concatenated ``/proc/net`` dump into rows."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(NET_SECTION_PREFIX) and stripped.endswith("__"):
            current = stripped[len(NET_SECTION_PREFIX) : -2]
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    connections: list[dict[str, Any]] = []
    per_file: dict[str, int] = {}
    for filename in PROC_NET_FILES:
        protocol = "tcp" if filename.startswith("tcp") else "udp"
        ipv6 = filename.endswith("6")
        states = UDP_STATES if protocol == "udp" else TCP_STATES
        count_before = len(connections)

        for line in sections.get(filename, []):
            fields = line.split()
            if len(fields) < 11 or not fields[0].rstrip(":").isdigit():
                continue
            if ":" not in fields[1] or ":" not in fields[2]:
                continue

            local_address, local_port = _decode_proc_endpoint(fields[1], ipv6)
            remote_address, remote_port = _decode_proc_endpoint(fields[2], ipv6)
            state_code = fields[3].upper()

            try:
                tx_hex, rx_hex = fields[4].split(":", 1)
                tx_queue, rx_queue = int(tx_hex, 16), int(rx_hex, 16)
            except ValueError:
                tx_queue = rx_queue = 0

            try:
                uid = int(fields[8])
                inode = int(fields[9])  # inode is in field 9, not 10
            except ValueError:
                uid = inode = 0

            log.debug(
                "Parsed connection: local=%s:%s remote=%s:%s state=%s inode=%s uid=%s fields=%d",
                local_address, local_port, remote_address, remote_port, state_code, inode, uid, len(fields)
            )

            connections.append(
                {
                    "protocol": protocol,
                    "family": "ipv6" if ipv6 else "ipv4",
                    "state": states.get(state_code, f"UNKNOWN_{state_code}"),
                    "state_code": state_code,
                    "local_address": local_address,
                    "local_port": local_port,
                    "remote_address": remote_address,
                    "remote_port": remote_port,
                    "uid": uid,
                    "inode": inode,
                    "tx_queue": tx_queue,
                    "rx_queue": rx_queue,
                }
            )
            per_file[filename] = per_file.get(filename, 0) + len(connections) - count_before

    log.debug(
        "parse_proc_net: per-file counts=%s total=%d",
        per_file,
        len(connections),
    )

    connections.sort(
        key=lambda item: (
            0 if item["protocol"] == "tcp" else 1,
            STATE_PRIORITY.get(item["state"], 99),
            0 if item["family"] == "ipv4" else 1,
            item["local_port"],
            item["local_address"],
            item["remote_port"],
        )
    )
    return connections


def _self_container_ids() -> list[str]:
    """Best-effort list of identifiers that may resolve to this container."""
    identifiers: list[str] = []

    def add(value: str | None) -> None:
        value = (value or "").strip()
        if value and value not in identifiers:
            identifiers.append(value)

    add(socket.gethostname())
    add(os.environ.get("HOSTNAME"))
    for path in ("/proc/self/cgroup", "/proc/self/mountinfo"):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        for match in re.findall(r"[0-9a-f]{64}", text):
            add(match)
    return identifiers


def _find_helper_image(client: docker.DockerClient) -> str:
    """Locate an image with a shell to run the ``/proc/net`` helper.

    The extension image is used because it is guaranteed to be present in the
    Desktop VM and it contains ``/bin/sh`` and ``/bin/cat``. It can be pinned
    with ``IMGFASTLER_HELPER_IMAGE`` (set from ``${DESKTOP_PLUGIN_IMAGE}`` in
    ``docker-compose.yaml``) and otherwise falls back to the image label.
    """
    candidates: list[str] = []
    override = os.environ.get("IMGFASTLER_HELPER_IMAGE", "").strip()
    if override:
        candidates.append(override)

    for identifier in _self_container_ids():
        try:
            image_id = (client.containers.get(identifier).attrs or {}).get("Image")
        except DockerException:
            continue
        if image_id:
            candidates.append(image_id)

    try:
        images = client.images.list(
            filters={"label": "org.opencontainers.image.title=imgfastler"}
        )
    except DockerException:
        images = []
    images.sort(key=lambda image: (image.attrs or {}).get("Created", 0), reverse=True)
    candidates.extend(image.id for image in images)

    for candidate in dict.fromkeys(candidates):
        try:
            client.images.get(candidate)
        except DockerException:
            continue
        return candidate

    raise HTTPException(
        status_code=500,
        detail=(
            "Cannot locate the imgfastler image needed to inspect the container "
            "network namespace."
        ),
    )


def _dump_container_sockets(container: Any) -> str:
    """Read ``/proc/net/{tcp,tcp6,udp,udp6}`` from a container's netns using docker exec."""
    try:
        # Use docker exec to read the files directly from the container
        output_parts = []
        for filename in PROC_NET_FILES:
            path = f"/proc/net/{filename}"
            try:
                result = container.exec_run(["cat", path], privileged=True)
                content = result[1].decode("utf-8", "replace") if result[1] else ""
                output_parts.append(f"__IMFASTLER_{filename}__\n{content}")
            except Exception as e:
                output_parts.append(f"__IMFASTLER_{filename}__\n# Error reading {path}: {str(e)}")

        return "\n".join(output_parts)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not read container network namespace: {str(exc)}",
        ) from exc


# --- routes ------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        engine = get_client()
        version = engine.version()
        return {
            "status": "ok",
            "engine": {
                "version": version.get("Version"),
                "api_version": version.get("ApiVersion"),
                "os": version.get("Os"),
                "arch": version.get("Arch"),
            },
        }
    except DockerException as exc:
        raise as_http_error(exc) from exc


@app.get("/api/images")
def list_images() -> list[dict[str, Any]]:
    try:
        images = get_client().images.list()
    except DockerException as exc:
        raise as_http_error(exc) from exc

    result = [
        {
            "id": image.id,
            "short_id": image.short_id.replace("sha256:", "")[:12],
            "tags": image.tags or [],
            "size": (image.attrs or {}).get("Size", 0),
            "virtual_size": (image.attrs or {}).get("VirtualSize", 0),
            "created": _created_timestamp((image.attrs or {}).get("Created")),
            "in_use": (image.attrs or {}).get("Containers", 0),
            "shared_size": (image.attrs or {}).get("SharedSize", 0),
        }
        for image in images
    ]
    result.sort(key=lambda item: item["created"], reverse=True)
    return result


@app.delete("/api/images/{name:path}")
def remove_image(name: str, force: bool = False) -> dict[str, Any]:
    try:
        image = get_client().images.get(name)
        removed = image.remove(force=force)
    except DockerException as exc:
        raise as_http_error(exc) from exc
    return {"removed": removed, "name": name}


@app.get("/api/networks")
def list_networks() -> list[dict[str, Any]]:
    try:
        client = get_client()
        networks = client.networks.list()
    except DockerException as exc:
        raise as_http_error(exc) from exc

    result = []
    for network in networks:
        try:
            # Inspect network to get full details including containers
            network_inspect = client.api.inspect_network(network.id)
            attrs = network_inspect or network.attrs or {}
        except Exception:
            attrs = network.attrs or {}

        ipam = attrs.get("IPAM") or {}
        config = (ipam.get("Config") or [{}])[0]
        containers_raw = attrs.get("Containers") or {}
        containers = []
        for container_id, info in containers_raw.items():
            containers.append(
                {
                    "id": container_id,
                    "name": info.get("Name", ""),
                    "ipv4_address": _split_cidr(info.get("IPv4Address", ""))[0]
                    if info.get("IPv4Address")
                    else None,
                    "ipv6_address": _split_cidr(info.get("IPv6Address", ""))[0]
                    if info.get("IPv6Address")
                    else None,
                    "mac_address": info.get("MacAddress", ""),
                }
            )
        containers.sort(key=lambda item: (item["name"] or "", item["id"]))
        result.append(
            {
                "id": network.id,
                "name": network.name,
                "driver": attrs.get("Driver", ""),
                "scope": attrs.get("Scope", ""),
                "internal": attrs.get("Internal", False),
                "subnet": config.get("Subnet"),
                "gateway": config.get("Gateway"),
                "ipv6_subnet": _find_ipv6_subnet(config),
                "containers": containers,
                "container_count": len(containers),
            }
        )
    result.sort(key=lambda item: (item["name"] != "bridge", item["name"]))
    return result


def _split_cidr(cidr: str) -> tuple[str, str | None]:
    """Split an IPv4/IPv6 CIDR string into (address, prefix_len)."""
    if "/" in cidr:
        address, prefix = cidr.split("/", 1)
        return address, prefix
    return cidr, None


def _find_ipv6_subnet(config: dict[str, Any]) -> str | None:
    """Find an IPv6 subnet from IPAM config entries."""
    for entry in config if isinstance(config, list) else [config]:
        subnet = entry.get("Subnet", "") if isinstance(entry, dict) else ""
        if subnet and ":" in subnet:
            return subnet
    return None


@app.get("/api/volumes")
def list_volumes() -> list[dict[str, Any]]:
    try:
        volumes = get_client().volumes.list()
    except DockerException as exc:
        raise as_http_error(exc) from exc

    return [
        {
            "name": volume.name,
            "driver": volume.attrs.get("Driver", ""),
            "mountpoint": volume.attrs.get("Mountpoint", ""),
            "created": _created_timestamp(volume.attrs.get("CreatedAt")),
        }
        for volume in volumes
    ]


@app.get("/api/containers")
def list_containers(all: bool = True) -> list[dict[str, Any]]:
    try:
        containers = get_client().containers.list(all=all)
    except DockerException as exc:
        raise as_http_error(exc) from exc

    result = []
    for container in containers:
        attrs = container.attrs or {}
        config = attrs.get("Config", {}) or {}
        host_config = attrs.get("HostConfig", {}) or {}
        network_settings = attrs.get("NetworkSettings", {}) or {}
        mounts = host_config.get("Mounts") or []

        result.append(
            {
                "id": container.id,
                "short_id": container.short_id[:12],
                "name": container.name,
                "image": config.get("Image", ""),
                "state": attrs.get("State", {}).get("Status", ""),
                "status": container.status,
                "created": _created_timestamp(attrs.get("Created")),
                "command": " ".join(config.get("Cmd") or []),
                "entrypoint": " ".join(config.get("Entrypoint") or []),
                "hostname": config.get("Hostname", ""),
                "ports": _port_list(network_settings.get("Ports") or {}),
                "ports_structured": _ports_structured(
                    network_settings.get("Ports") or {}
                ),
                "networks": sorted((network_settings.get("Networks") or {}).keys()),
                "environment": config.get("Env") or [],
                "mounts": [
                    {
                        "type": m.get("Type", ""),
                        "source": m.get("Source", ""),
                        "target": m.get("Destination", ""),
                        # Docker reports read-only state as `ReadOnly` in
                        # `HostConfig.Mounts` (the Mounts API imgfastler uses);
                        # `RW` appears in the legacy top-level `Mounts`. Accept both.
                        "read_only": bool(m.get("ReadOnly")) or m.get("RW") is False,
                    }
                    for m in mounts
                ],
                "restart_policy": (host_config.get("RestartPolicy") or {}).get(
                    "Name", "no"
                ),
            }
        )
    result.sort(key=lambda item: item["created"], reverse=True)
    return result


def _build_ip_name_map(client: Any) -> dict[str, str]:
    """Map container IP addresses to their container names."""
    result: dict[str, str] = {}
    try:
        for container in client.containers.list(all=True):
            networks = (
                (container.attrs or {}).get("NetworkSettings", {}) or {}
            ).get("Networks") or {}
            for network in networks.values():
                ip_address = (network or {}).get("IPAddress")
                if ip_address and ip_address not in result:
                    result[ip_address] = container.name
    except DockerException:
        pass
    return result


def _resolve_ip_address(client: Any, ip: str, ip_name_map: dict[str, str] | None = None) -> str:
    """Resolve an IP address to a human-readable name on the backend."""
    if not ip or ip in {"0.0.0.0", "::", "::1"}:
        return ip
    if ip_name_map is None:
        try:
            ip_name_map = _build_ip_name_map(client)
        except Exception:  # pragma: no cover
            ip_name_map = {}
    name = (ip_name_map or {}).get(ip)
    if name:
        return name
    try:
        hostname = socket.gethostbyaddr(ip)[0]
        if hostname and hostname != ip:
            return hostname
    except (socket.herror, socket.gaierror, OSError):
        pass
    return ip


@app.get("/api/containers/{container_id}/connections")
def container_connections(
    container_id: str,
    resolve: bool = False,
    resolve_ips: bool = False,
    resolve_ip: bool = False,
    resolved: bool = False,
) -> dict[str, Any]:
    """List all TCP/UDP sockets in a container's network namespace."""
    do_resolve = resolve or resolve_ips or resolve_ip or resolved
    client = get_client()
    try:
        container = client.containers.get(container_id)
    except DockerException as exc:
        raise as_http_error(exc) from exc

    attrs = container.attrs or {}
    container_state = attrs.get("State", {}) or {}
    status = container_state.get("Status", "") or container.status
    pid = container_state.get("Pid") or 0
    if status not in {"running", "paused"} or not pid:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Container '{container.name}' is {status or 'not running'}; its "
                "network namespace is only available while it is running or paused."
            ),
        )

    host_config = attrs.get("HostConfig", {}) or {}
    network_settings = attrs.get("NetworkSettings", {}) or {}
    sandbox_key = network_settings.get("SandboxKey") or ""
    host_network = host_config.get("NetworkMode") == "host"

    # Docker shares one network namespace per `NetworkSettings.SandboxKey`
    # (host networking and `--network container:<id>` both do this).
    shared_with: list[dict[str, str]] = []
    if sandbox_key:
        try:
            for other in client.containers.list(all=True):
                if other.id == container.id:
                    continue
                other_settings = (other.attrs or {}).get("NetworkSettings") or {}
                if other_settings.get("SandboxKey") == sandbox_key:
                    shared_with.append(
                        {
                            "id": other.id,
                            "short_id": other.short_id[:12],
                            "name": other.name,
                            "state": ((other.attrs or {}).get("State") or {}).get(
                                "Status", ""
                            ),
                        }
                    )
        except DockerException:
            # Losing the context list is not fatal for the connection view.
            shared_with = []

    raw_output = _dump_container_sockets(container)
    log.info(
        "container_connections: %s raw_output chars=%d head=%r",
        container_id,
        len(raw_output or ""),
        (raw_output or "")[:200],
    )
    connections = _parse_proc_net(raw_output)
    ip_name_map = _build_ip_name_map(client)
    for connection in connections:
        connection["local_resolved"] = _resolve_ip_address(client, connection["local_address"], ip_name_map)
        connection["remote_resolved"] = _resolve_ip_address(client, connection["remote_address"], ip_name_map)
    log.info(
        "container_connections: %s -> %d connections",
        container_id,
        len(connections),
    )
    if not connections and raw_output and len(raw_output.strip()) > 0:
        log.warning(
            "container_connections: %s produced raw output (%d chars) but parsed 0 connections; "
            "likely a /proc/net format parsing mismatch",
            container_id,
            len(raw_output),
        )
    summary = {
        "total": len(connections),
        "tcp": sum(1 for item in connections if item["protocol"] == "tcp"),
        "udp": sum(1 for item in connections if item["protocol"] == "udp"),
        "ipv4": sum(1 for item in connections if item["family"] == "ipv4"),
        "ipv6": sum(1 for item in connections if item["family"] == "ipv6"),
        "listening": sum(1 for item in connections if item["state"] == "LISTEN"),
        "established": sum(
            1 for item in connections if item["state"] == "ESTABLISHED"
        ),
    }

    return {
        "container": {
            "id": container.id,
            "short_id": container.short_id[:12],
            "name": container.name,
            "image": (attrs.get("Config") or {}).get("Image", ""),
            "state": status,
        },
        "netns": {
            "pid": pid,
            "sandbox_key": sandbox_key,
            "host_network": host_network,
            "shared_with": shared_with,
        },
        "summary": summary,
        "connections": connections,
        "generated_at": int(datetime.now().timestamp()),
    }


def _build_networks_state(
    client: docker.DockerClient, network_names: list[str]
) -> list[dict[str, Any]]:
    """Report containers connected to each network for UI context."""
    networks_state: list[dict[str, Any]] = []
    for network_name in network_names:
        try:
            net = client.networks.get(network_name)
            networks_state.append(
                {
                    "name": network_name,
                    "containers": [
                        {
                            "id": c.id,
                            "name": c.name,
                            "state": (c.attrs or {}).get("State", {}).get("Status", ""),
                        }
                        for c in net.containers
                    ],
                }
            )
        except DockerException as exc:
            raise as_http_error(exc) from exc
    return networks_state


def _ensure_volumes_exist(
    client: docker.DockerClient, mounts: list[MountSpec]
) -> set[str]:
    """Create named volumes on demand, return set of created volume names."""
    existing_volumes: set[str] = set()
    for mount in mounts:
        if mount.type != "volume" or mount.source in existing_volumes:
            continue
        try:
            client.volumes.get(mount.source)
        except NotFound:
            client.volumes.create(name=mount.source)
        except DockerException as exc:
            raise as_http_error(exc) from exc
        existing_volumes.add(mount.source)
    return existing_volumes


def _build_port_bindings(ports: list[PortSpec]) -> dict[str, Any]:
    """Convert PortSpec list to Docker port bindings dict."""
    bindings: dict[str, Any] = {}
    for port in ports:
        key = f"{port.container_port}/{port.protocol}"
        if port.host_port:
            bindings[key] = (port.host_ip or "0.0.0.0", port.host_port)
        else:
            bindings[key] = None  # random host port (docker run -P style)
    return bindings


@app.post("/api/containers", status_code=201)
def create_container(spec: ContainerSpec) -> dict[str, Any]:
    """Validate the payload and create + start a container."""
    client = get_client()

    # Validate image exists locally (no surprise pulls)
    try:
        client.images.get(spec.image)
    except DockerException as exc:
        raise as_http_error(exc) from exc

    # Validate all requested networks exist
    networks = list(dict.fromkeys(spec.networks))  # de-dup, keep order
    for network_name in networks:
        try:
            client.networks.get(network_name)
        except DockerException as exc:
            raise HTTPException(
                status_code=404,
                detail=f"Network '{network_name}' not found: {_extract_error_message(exc)}",
            ) from exc

    # Build container kwargs
    kwargs: dict[str, Any] = {"detach": True}

    if spec.name:
        kwargs["name"] = spec.name
    if spec.command:
        kwargs["command"] = shlex.split(spec.command)
    if spec.entrypoint:
        kwargs["entrypoint"] = shlex.split(spec.entrypoint)
    if spec.hostname:
        kwargs["hostname"] = spec.hostname
    if spec.environment:
        kwargs["environment"] = dict(spec.environment)
    if spec.restart_policy != "no":
        kwargs["restart_policy"] = {"Name": spec.restart_policy, "MaximumRetryCount": 0}
    if spec.ports:
        kwargs["ports"] = _build_port_bindings(spec.ports)
    if spec.mounts:
        _ensure_volumes_exist(client, spec.mounts)
        kwargs["mounts"] = [
            Mount(
                target=mount.target,
                source=mount.source,
                type=mount.type,
                read_only=mount.read_only,
            )
            for mount in spec.mounts
        ]
    if networks:
        kwargs["network"] = networks[0]

    # Get network state for UI context
    networks_state = _build_networks_state(client, networks)

    # Create container
    try:
        container = client.containers.run(spec.image, **kwargs)
    except DockerException as exc:
        raise as_http_error(exc) from exc

    # Attach extra networks
    warnings: list[str] = []
    for network_name in networks[1:]:
        try:
            client.networks.get(network_name).connect(container)
        except DockerException as exc:
            warnings.append(
                f"Could not attach to network '{network_name}': {_extract_error_message(exc)}"
            )

    container.reload()
    attrs = container.attrs or {}
    return {
        "id": container.id,
        "short_id": container.short_id[:12],
        "name": container.name,
        "image": spec.image,
        "state": attrs.get("State", {}).get("Status", ""),
        "warnings": warnings,
        "networks": networks_state,
    }


@app.post("/api/containers/{container_id}/start")
def start_container(container_id: str) -> dict[str, str]:
    try:
        container = get_client().containers.get(container_id)
        container.start()
    except DockerException as exc:
        raise as_http_error(exc) from exc
    return {"id": container.id, "state": "started"}


@app.post("/api/containers/{container_id}/restart")
def restart_container(container_id: str) -> dict[str, str]:
    try:
        container = get_client().containers.get(container_id)
        container.restart()
    except DockerException as exc:
        raise as_http_error(exc) from exc
    return {"id": container.id, "state": "restarting"}


@app.post("/api/containers/{container_id}/stop")
def stop_container(container_id: str) -> dict[str, str]:
    try:
        container = get_client().containers.get(container_id)
        container.stop(timeout=STOP_TIMEOUT)
    except DockerException as exc:
        raise as_http_error(exc) from exc
    return {"id": container.id, "state": "stopped"}


@app.delete("/api/containers/{container_id}")
def remove_container(container_id: str, force: bool = True) -> dict[str, Any]:
    try:
        container = get_client().containers.get(container_id)
        container.remove(force=force)
    except DockerException as exc:
        raise as_http_error(exc) from exc
    return {"removed": container_id}


@app.get("/api/containers/{container_id}/logs")
async def container_logs(container_id: str, follow: bool = False, tail: int | None = None) -> Any:
    """Return container logs; `follow=true` streams new output as SSE; `tail=N` limits to last N lines."""
    try:
        container = get_client().containers.get(container_id)
    except DockerException as exc:
        raise as_http_error(exc) from exc

    if follow:

        def event_stream():
            buffer = b""
            for chunk in container.logs(
                stream=True, follow=True, stdout=True, stderr=True, tail=tail
            ):
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    yield f"event: log\ndata: {line.decode('utf-8', 'replace')}\n\n"
            if buffer:
                yield f"event: log\ndata: {buffer.decode('utf-8', 'replace')}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    raw = container.logs(stream=False, stdout=True, stderr=True, tail=tail)
    text = (
        raw.decode("utf-8", "replace")
        if isinstance(raw, (bytes, bytearray))
        else str(raw)
    )
    lines = [line for line in text.split("\n") if line] if text else []
    return {"logs": lines}


@app.post("/api/containers/{container_id}/connections/kill")
def kill_connection(container_id: str, inode: int, local_address: str | None = None, local_port: int | None = None, remote_address: str | None = None, remote_port: int | None = None) -> dict[str, Any]:
    """Kill a network connection on the host system using ss -K."""
    try:
        container = get_client().containers.get(container_id)
    except DockerException as exc:
        raise as_http_error(exc) from exc

    debug_info = []

    try:
        # Get connection details from container if not provided
        if not all([local_address, local_port, remote_address, remote_port]):
            get_conn_script = f"""
            if command -v ss >/dev/null 2>&1; then
                ss -tuna | grep "inode={inode}" || echo "Not found"
            fi
            """
            conn_output = container.exec_run(["/bin/sh", "-c", get_conn_script], privileged=True)
            conn_text = conn_output[1].decode("utf-8", "replace")
            debug_info.append(f"Connection details: {conn_text}")

            # Parse connection details
            import re
            match = re.search(r"(\S+):(\d+)\s+(\S+):(\d+)", conn_text)
            if match:
                local_address, local_port, remote_address, remote_port = match.groups()
                local_port = int(local_port)
                remote_port = int(remote_port)

        # Only proceed if we have connection details
        if not all([local_address, local_port, remote_address, remote_port]):
            return {
                "inode": inode,
                "killed": False,
                "debug": debug_info,
                "message": "Could not determine connection details"
            }

        # Try to kill connection using socket-level methods only (no process killing)
        debug_info.append(f"=== Attempting inter-container kill for {local_address}:{local_port} -> {remote_address}:{remote_port} (inode: {inode}) ===")

        try:
            # Convert IP addresses to hex format for /proc/net/tcp
            def ip_to_hex(ip: str) -> str:
                parts = ip.split('.')
                if len(parts) == 4:
                    return ''.join(f'{int(p):02X}' for p in reversed(parts))
                return ip

            local_hex = ip_to_hex(local_address)
            remote_hex = ip_to_hex(remote_address)
            local_port_hex = f'{local_port:04X}'
            remote_port_hex = f'{remote_port:04X}'

            # Method 1: Find connection in /proc/net/tcp and get its inode
            find_conn_cmd = f"""
            # Search for the connection in /proc/net/tcp
            echo "Searching for {local_address}:{local_port} -> {remote_address}:{remote_port}"
            echo "Local hex: {local_hex}:{local_port_hex}, Remote hex: {remote_hex}:{remote_port_hex}"
            grep "{local_hex}:{local_port_hex}" /proc/net/tcp || \
            grep "{remote_hex}:{remote_port_hex}" /proc/net/tcp || \
            grep "{local_hex}" /proc/net/tcp | grep "{local_port_hex}" || \
            grep "{remote_hex}" /proc/net/tcp | grep "{remote_port_hex}"
            """
            result = container.exec_run(["/bin/sh", "-c", find_conn_cmd], privileged=True)
            conn_output = result[1].decode("utf-8", "replace") if result[1] else ""
            debug_info.append(f"Connection search output:\n{conn_output}")

            # Parse the found connection to get the actual inode
            actual_inode = None
            if conn_output.strip():
                import re
                # Split by whitespace and get field 9 (0-indexed) which is the inode
                lines = conn_output.strip().split('\n')
                for line in lines:
                    fields = line.split()
                    if len(fields) > 9:
                        # Field 9 is the inode in /proc/net/tcp format
                        actual_inode = fields[9]
                        debug_info.append(f"Found actual inode: {actual_inode} from line: {line}")
                        break

            # Method 2: Try ss -K if available (socket-level kill)
            ss_check = container.exec_run(["sh", "-c", "command -v ss || echo 'ss not found'"], privileged=True)
            ss_available = ss_check[1].decode("utf-8", "replace").strip()
            debug_info.append(f"ss check: {ss_available}")

            if "ss not found" not in ss_available:
                ss_cmds = [
                    f'ss -K "inode = {actual_inode or inode}"',
                    f'ss -K "( sport = :{local_port} and dport = :{remote_port} )"',
                    f'ss -K "src {local_address}:{local_port} dst {remote_address}:{remote_port}"',
                    f'ss -K "local {local_address}:{local_port} peer {remote_address}:{remote_port}"',
                ]
                for i, cmd in enumerate(ss_cmds, 1):
                    result = container.exec_run(["/bin/sh", "-c", f"{cmd} 2>&1"], privileged=True)
                    output = result[1].decode("utf-8", "replace") if result[1] else ""
                    debug_info.append(f"ss -K command {i} output:\n{output}")

                    if result[0] == 0 and "killed" in output.lower():
                        return {
                            "inode": inode,
                            "killed": True,
                            "debug": debug_info,
                            "message": f"Connection killed via ss -K (command {i})"
                        }

            # Method 3: Use nicolaka/netshoot image connected to target container's network
            if actual_inode:
                try:
                    client = get_client()
                    helper = None
                    try:
                        # Use nicolaka/netshoot which has all network tools pre-installed
                        # Connect to the target container's network namespace
                        kill_script = f'''
set -e
echo "=== Netshoot kill attempt for {local_address}:{local_port} -> {remote_address}:{remote_port} ==="
echo "Inode: {actual_inode or inode}"

# Method 1: Try ss -K with various syntax
for cmd in \\
    "ss -K 'inode = {actual_inode or inode}'" \\
    "ss -K '( sport = :{local_port} and dport = :{remote_port} )'" \\
    "ss -K 'src {local_address}:{local_port} dst {remote_address}:{remote_port}'" \\
    "ss -K 'local {local_address}:{local_port} peer {remote_address}:{remote_port}'" \\
    "ss -K 'dst {remote_address}:{remote_port}'" \\
    "ss -K 'src {local_address}:{local_port}'"; do
    echo "Trying: $cmd"
    eval "$cmd" 2>&1 && echo "SUCCESS: $cmd" && exit 0
done

# Method 2: Try tcpkill
echo "Trying tcpkill..."
tcpkill -9 "{local_address}:{local_port}" "{remote_address}:{remote_port}" 2>&1 && echo "SUCCESS: tcpkill" && exit 0
tcpkill -9 "{local_address}" "{remote_address}" 2>&1 && echo "SUCCESS: tcpkill" && exit 0

# Method 3: Try to find and close socket via /proc
echo "Trying /proc method..."
# Find the socket inode in /proc/net/tcp
grep "{local_hex}:{local_port_hex}" /proc/net/tcp | grep "{remote_hex}:{remote_port_hex}" || \\
grep "{remote_hex}:{remote_port_hex}" /proc/net/tcp | grep "{local_hex}:{local_port_hex}"

echo "All methods failed"
exit 1
'''

                        helper = client.containers.run(
                            "nicolaka/netshoot:latest",
                            command=["/bin/sh", "-c", kill_script],
                            network_mode=f"container:{container_id}",
                            pid_mode=f"container:{container_id}",
                            privileged=True,
                            detach=True,
                            remove=False,
                        )
                        helper.wait(timeout=250)  # Longer timeout for image pull
                        output = helper.logs(stdout=True, stderr=True)
                        kill_output = output.decode("utf-8", "replace") if output else ""
                        debug_info.append(f"netshoot kill output:\n{kill_output}")

                        if "SUCCESS" in kill_output:
                            return {
                                "inode": inode,
                                "killed": True,
                                "debug": debug_info,
                                "message": "Connection killed via netshoot"
                            }

                    except Exception as e:
                        debug_info.append(f"netshoot exception: {str(e)}")
                    finally:
                        if helper is not None:
                            try:
                                helper.remove(force=True)
                            except DockerException:
                                pass
                except Exception as e:
                    debug_info.append(f"netshoot setup exception: {str(e)}")

            return {
                "inode": inode,
                "killed": False,
                "debug": debug_info,
                "message": "Could not kill connection - no socket-level method available"
            }
        except Exception as e:
            debug_info.append(f"Container exec exception: {str(e)}")
            return {
                "inode": inode,
                "killed": False,
                "debug": debug_info,
                "message": f"Container exec kill failed: {str(e)}"
            }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to kill connection: {str(exc)}\nDebug info: {debug_info}",
        ) from exc


@app.get("/api/resolve")
def resolve_ip(ip: str) -> dict[str, str]:
    """Resolve an IP address to a hostname/container name on the backend."""
    try:
        client = get_client()
    except Exception:
        client = None
    hostname = _resolve_ip_address(client, ip) if client is not None else ip
    return {"ip": ip, "hostname": hostname}


# --- entrypoint --------------------------------------------------------


def main() -> None:
    test_port = int(os.environ.get("TEST_PORT", "0") or 0)
    log_level = os.environ.get("LOG_LEVEL", "info").lower()

    if test_port:
        # Local development mode: plain TCP server.
        log.info("Starting backend in test mode on 127.0.0.1:%d", test_port)
        uvicorn.run(app, host="127.0.0.1", port=test_port, log_level=log_level)
        return

    socket_path = os.environ.get("EXTENSION_SOCKET", DEFAULT_SOCKET)
    directory = os.path.dirname(socket_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    if os.path.exists(socket_path):
        os.remove(socket_path)  # stale socket left by a previous run

    log.info("Starting backend on unix socket %s", socket_path)
    # uvicorn's Server applies 0o666 to the socket it creates for us.
    uvicorn.run(app, uds=socket_path, log_level=log_level)


if __name__ == "__main__":
    main()
