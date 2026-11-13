"""Unit tests for creating containers (POST /api/containers).

The docker-py client is replaced by a fake, so these tests run without a
Docker daemon and verify what the route *asks* the engine to do.

Requires: pytest, httpx (TestClient), plus the backend requirements.

    .venv/bin/python -m pip install pytest httpx
    .venv/bin/python -m pytest backend/tests -v
"""

from __future__ import annotations

import re
from typing import Any

import docker
import pytest
import server
from docker.errors import APIError, NotFound
from docker.types import Mount
from fastapi.testclient import TestClient

IMAGE = "alpine:3.20"


class FakeContainer:
    def __init__(self, name: str = "fake-1") -> None:
        self.id = "abc123def456abc123def456abc123de"
        self.short_id = self.id[:10]
        self.name = name
        self.attrs: dict[str, Any] = {"State": {"Status": "running"}}

    def reload(self) -> None:
        pass


class FakeNetwork:
    def __init__(self, name: str, containers: list[Any] | None = None) -> None:
        self.name = name
        self.containers = containers or []
        self.connected: list[Any] = []

    def connect(self, container: Any) -> None:
        self.connected.append(container)


class FakeVolume:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeDocker:
    """Minimal stand-in for docker.DockerClient, recording what is called."""

    def __init__(
        self,
        missing_images: set[str] | None = None,
        missing_networks: set[str] | None = None,
    ) -> None:
        self.missing_images = missing_images or set()
        self.missing_networks = missing_networks or set()
        self.container = FakeContainer()
        self.volumes_created: list[str] = []
        self.run_calls: list[tuple[str, dict[str, Any]]] = []
        self.images = _FakeImages(self)
        self.networks = _FakeNetworks(self)
        self.volumes = _FakeVolumes(self)
        self.containers = _FakeContainers(self)


class _FakeImages:
    def __init__(self, state: FakeDocker) -> None:
        self._state = state

    def get(self, name: str) -> dict[str, Any]:
        if name in self._state.missing_images:
            raise NotFound(f"No such image: {name}")
        return {"id": "sha256:" + "0" * 64}


class _FakeNetworks:
    def __init__(self, state: FakeDocker) -> None:
        self._state = state
        self.by_name: dict[str, FakeNetwork] = {}

    def get(self, name: str) -> FakeNetwork:
        if name in self._state.missing_networks:
            raise NotFound(f"network '{name}' not found")
        if name not in self.by_name:
            self.by_name[name] = FakeNetwork(name)
        return self.by_name[name]

    def create(self, name: str) -> FakeNetwork:
        return self.get(name)


class _FakeVolumes:
    def __init__(self, state: FakeDocker) -> None:
        self._state = state

    def get(self, name: str) -> FakeVolume:
        if name not in self._state.volumes_created:
            raise NotFound(f"No such volume: {name}")
        return FakeVolume(name)

    def create(self, name: str) -> FakeVolume:
        self._state.volumes_created.append(name)
        return FakeVolume(name)


class _FakeContainers:
    def __init__(self, state: FakeDocker) -> None:
        self._state = state

    def run(self, image: str, **kwargs: Any) -> FakeContainer:
        self._state.run_calls.append((image, kwargs))
        return self._state.container

    def get(self, container_id: str) -> FakeContainer:  # noqa: ARG002 - API parity
        return self._state.container


@pytest.fixture()
def fake() -> FakeDocker:
    return FakeDocker()


@pytest.fixture()
def api(monkeypatch: pytest.MonkeyPatch, fake: FakeDocker) -> TestClient:
    monkeypatch.setattr(server, "get_client", lambda: fake)
    return TestClient(server.app)


# --- validation rejects bad payloads -------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"image": ""},  # empty image
        {"ports": [{"container_port": 0}]},  # port < 1
        {"ports": [{"container_port": 70000}]},  # port > 65535
        {"ports": [{"container_port": 80, "host_port": 70000}]},
        {"ports": [{"container_port": 80, "protocol": "icmp"}]},
        {"restart_policy": "sometimes"},  # not a valid policy
        {"mounts": [{"source": "", "target": "/x"}]},  # empty bind source
        {"mounts": [{"source": "data", "target": ""}]},
        {"environment": {"A": 1}},  # env values must be strings
    ],
)
def test_invalid_payload_rejected_with_422(api: TestClient, payload: dict) -> None:
    body = {"image": IMAGE, **payload}
    response = api.post("/api/containers", json=body)
    assert response.status_code == 422, response.text


# --- happy paths ---------------------------------------------------------


def test_create_minimal(api: TestClient, fake: FakeDocker) -> None:
    response = api.post("/api/containers", json={"image": IMAGE})
    assert response.status_code == 201, response.text
    data = response.json()

    assert data["image"] == IMAGE
    assert data["state"] == "running"
    assert data["warnings"] == []
    assert re.fullmatch(r"[0-9a-f]{10}", data["short_id"])

    # only one engine call, with detach and nothing else
    assert len(fake.run_calls) == 1
    image, kwargs = fake.run_calls[0]
    assert image == IMAGE
    assert kwargs == {"detach": True}


def test_create_full_spec(api: TestClient, fake: FakeDocker) -> None:
    spec = {
        "image": IMAGE,
        "name": "my-nginx",
        "command": "nginx -g 'daemon off;'",
        "entrypoint": "/docker-entrypoint.sh --flag",
        "hostname": "my-nginx",
        "environment": {"TZ": "Europe/Berlin"},
        "restart_policy": "always",
        "ports": [
            {
                "host_port": 8080,
                "container_port": 80,
                "host_ip": "127.0.0.1",
                "protocol": "tcp",
            },
            {"container_port": 443, "protocol": "udp"},
        ],
        "mounts": [
            {
                "type": "bind",
                "source": "/etc/nginx",
                "target": "/etc/nginx",
                "read_only": True,
            },
            {"type": "volume", "source": "data-vol", "target": "/var/lib/data"},
        ],
        "networks": ["bridge", "my-net"],
    }
    response = api.post("/api/containers", json=spec)
    assert response.status_code == 201, response.text

    image, kwargs = fake.run_calls[0]
    assert image == IMAGE
    assert kwargs["detach"] is True
    assert kwargs["name"] == "my-nginx"
    # shell-split, quotes resolved
    assert kwargs["command"] == ["nginx", "-g", "daemon off;"]
    assert kwargs["entrypoint"] == ["/docker-entrypoint.sh", "--flag"]
    assert kwargs["hostname"] == "my-nginx"
    assert kwargs["environment"] == {"TZ": "Europe/Berlin"}
    assert kwargs["restart_policy"] == {"Name": "always", "MaximumRetryCount": 0}
    assert kwargs["ports"] == {
        "80/tcp": ("127.0.0.1", 8080),
        "443/udp": None,  # random host port
    }
    assert kwargs["network"] == "bridge"  # first network at create time
    mounts = kwargs["mounts"]
    assert all(isinstance(m, Mount) for m in mounts)
    assert [m["Target"] for m in mounts] == ["/etc/nginx", "/var/lib/data"]
    assert mounts[0]["ReadOnly"] is True
    assert mounts[1]["ReadOnly"] is False

    # extra network attached after create
    assert len(fake.networks.by_name["my-net"].connected) == 1
    assert fake.networks.by_name["my-net"].connected[0] is fake.container


def test_duplicate_networks_deduplicated(api: TestClient, fake: FakeDocker) -> None:
    response = api.post(
        "/api/containers", json={"image": IMAGE, "networks": ["bridge", "bridge"]}
    )
    assert response.status_code == 201, response.text

    _, kwargs = fake.run_calls[0]
    assert kwargs["network"] == "bridge"
    assert fake.networks.by_name["bridge"].connected == []  # attached once, at create


def test_named_volume_created_on_demand(api: TestClient, fake: FakeDocker) -> None:
    spec = {
        "image": IMAGE,
        "mounts": [{"type": "volume", "source": "data-vol", "target": "/data"}],
    }
    response = api.post("/api/containers", json=spec)
    assert response.status_code == 201, response.text
    assert fake.volumes_created == ["data-vol"]

    _, kwargs = fake.run_calls[0]
    assert kwargs["mounts"][0]["Source"] == "data-vol"


def test_existing_volume_not_recreated(api: TestClient, fake: FakeDocker) -> None:
    fake.volumes_created.append("data-vol")  # already exists
    response = api.post(
        "/api/containers",
        json={
            "image": IMAGE,
            "mounts": [{"type": "volume", "source": "data-vol", "target": "/data"}],
        },
    )
    assert response.status_code == 201, response.text
    assert fake.volumes_created == ["data-vol"]  # no second create


def test_duplicate_volume_mount_checked_once(api: TestClient, fake: FakeDocker) -> None:
    response = api.post(
        "/api/containers",
        json={
            "image": IMAGE,
            "mounts": [
                {"type": "volume", "source": "data-vol", "target": "/a"},
                {"type": "volume", "source": "data-vol", "target": "/b"},
            ],
        },
    )
    assert response.status_code == 201, response.text
    assert fake.volumes_created.count("data-vol") == 1


def test_failed_attach_becomes_warning(api: TestClient, fake: FakeDocker) -> None:
    def broken_connect(container: Any) -> None:  # noqa: ARG001 - API parity
        raise APIError("attach failed")

    net = FakeNetwork("my-net")
    net.connect = broken_connect  # type: ignore[method-assign]
    fake.networks.by_name["my-net"] = net

    response = api.post(
        "/api/containers", json={"image": IMAGE, "networks": ["bridge", "my-net"]}
    )
    assert response.status_code == 201, response.text
    warnings = response.json()["warnings"]
    assert len(warnings) == 1
    assert "Could not attach to network 'my-net'" in warnings[0]


def test_networks_reported_in_response(api: TestClient, fake: FakeDocker) -> None:
    net = FakeNetwork("bridge", containers=[fake.container])
    fake.networks.by_name["bridge"] = net

    response = api.post(
        "/api/containers", json={"image": IMAGE, "networks": ["bridge"]}
    )
    assert response.status_code == 201, response.text
    networks = response.json()["networks"]
    assert networks == [
        {
            "name": "bridge",
            "containers": [
                {
                    "id": fake.container.id,
                    "name": fake.container.name,
                    "state": "running",
                }
            ],
        }
    ]


# --- error paths ---------------------------------------------------------


def test_missing_image_rejected_before_create(
    api: TestClient, fake: FakeDocker
) -> None:
    fake.missing_images.add(IMAGE)
    response = api.post("/api/containers", json={"image": IMAGE})
    assert response.status_code == 404, response.text
    assert "No such image" in response.json()["detail"]
    assert fake.run_calls == []  # nothing was created


def test_missing_network_rejected_before_create(
    api: TestClient, fake: FakeDocker
) -> None:
    fake.missing_networks.add("no-such-net")
    response = api.post(
        "/api/containers", json={"image": IMAGE, "networks": ["no-such-net"]}
    )
    assert response.status_code == 404, response.text
    assert "Network 'no-such-net' not found" in response.json()["detail"]
    assert fake.run_calls == []


def test_engine_error_mapped_to_status(api: TestClient, fake: FakeDocker) -> None:
    class Response:
        status_code = 409
        url = "http://engine/v1.0/containers/create"
        reason = "Conflict"

    def fail_run(image: str, **kwargs: Any) -> FakeContainer:  # noqa: ARG001 - API parity
        raise APIError("port is already allocated", response=Response())

    fake.containers.run = fail_run  # type: ignore[method-assign]

    response = api.post("/api/containers", json={"image": IMAGE})
    assert response.status_code == 409, response.text
    # docker-py rewrites APIError messages to "<code> Client Error for <url>: <reason>"
    assert "409 Client Error" in response.json()["detail"]


def test_engine_unavailable_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    class Unavailable:
        @property
        def images(self) -> Any:
            raise docker.errors.DockerException("cannot connect to the Docker daemon")

    monkeypatch.setattr(server, "get_client", lambda: Unavailable())
    client = TestClient(server.app)
    response = client.post("/api/containers", json={"image": IMAGE})
    assert response.status_code == 503, response.text


# --- pure helper: _build_port_bindings ------------------------------------


def test_build_port_bindings() -> None:
    ports = [
        server.PortSpec(container_port=80, host_port=8080, host_ip="127.0.0.1"),
        server.PortSpec(container_port=443),
        server.PortSpec(container_port=53, protocol="udp"),
    ]
    assert server._build_port_bindings(ports) == {
        "80/tcp": ("127.0.0.1", 8080),
        "443/tcp": None,
        "53/udp": None,
    }


def test_build_port_bindings_defaults_host_ip() -> None:
    ports = [server.PortSpec(container_port=80, host_port=8080)]
    assert server._build_port_bindings(ports) == {"80/tcp": ("0.0.0.0", 8080)}
