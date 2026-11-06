# imgfastler — Docker Desktop extension

A Docker Desktop extension for working with local images and containers. Pick any image on the
main page → **Run** → configure **mounts, networks, environment variables and ports** → **Create
container**. Existing containers can be inspected, edited, restarted and removed from the same tab.

It contains all three extension components:

* **UI frontend** — static HTML/CSS/JS tab in the Docker Desktop dashboard
* **Backend** — a Python (FastAPI + docker-py) service running inside the Desktop VM, speaking to
  the Docker Engine over the VM's socket
* **Metafile** — `metadata.json`, the entry point Docker Desktop reads to install the extension

## Features

- **Images** — browse, search and delete local images; see tags, size, age and container usage.
  **Run** opens the container form pre-filled with the selected image.
- **Create container** — name, restart policy, command and entrypoint overrides, hostname,
  environment variables, port mappings (TCP/UDP, host IP, random host port), bind mounts and
  named volumes (including read-only mounts and on-demand volume creation), and multi-network
  attachment. The form shows which containers already use each network.
- **Containers** — list and search containers, start, stop, restart and remove them. **Edit**
  pre-fills the form from the current container and recreates it with the new settings.
- **Logs** — modal log viewer with tail support, follow toggle and auto-scroll.
- **Networks** — inspect networks with driver, IPv4/IPv6 subnet and container count; expand a
  network to see its containers and their addresses; filter by name, driver, subnet or IP.
- **Connections** — inspect TCP/UDP sockets in a container's network namespace (IPv4 and IPv6,
  socket states, UID and inode), optionally resolve addresses to container/host names, and
  attempt to kill a connection (experimental — see [Network connections](#network-connections)).
- **Live mode** — poll the active view every 5 seconds; the header also shows the engine version
  and the last refresh time.

---

## Layout

```
.
├── metadata.json              # metafile: dashboard tab + backend socket wiring
├── docker-compose.yaml        # backend service definition (adds the Docker socket)
├── docker-compose.dev.yaml    # standalone backend for local development
├── Dockerfile                 # packages ui/ + backend/ + metafile into one image
├── Makefile                   # build / install / dev / lint helpers
├── docker.svg                 # extension icon
├── pyproject.toml             # project metadata, ruff + mypy config
├── .ruff.toml                 # ruff lint/format rules
├── .pre-commit-config.yaml    # pre-commit hooks (ruff)
├── scripts/
│   └── smoke.py               # end-to-end API smoke test
├── ui/                        # frontend — extracted and served by Docker Desktop
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   └── logo.svg
├── backend/                   # backend — runs as a container in the Desktop VM
│   ├── server.py
│   └── requirements.txt
├── README.md
├── SETUP.md                   # note about sandboxed local backend execution
└── REFACTORING.md             # history of the code-quality refactor
```

| Component  | Where it runs                     | How the pieces connect                                                |
| ---------- | --------------------------------- | --------------------------------------------------------------------- |
| Frontend   | Electron (dashboard iframe)       | `window.ddClient.extension.vm.service.get/post/delete(...)`           |
| Metafile   | read by Docker Desktop at install | `ui.dashboard-tab` → tab, `vm.exposes.socket` → backend socket        |
| Backend    | container in the Desktop VM       | listens on `/run/guest-services/imgfastler.sock`                      |
| Engine API | the VM's Docker daemon            | `/var/run/docker.sock.raw` mounted as `/var/run/docker.sock`          |

`docker.sock.raw` (and not the host `/var/run/docker.sock`) is what makes the backend talk to the
daemon **inside** the VM — mounting the host socket from an extension container causes permission
issues.

---

## Backend API

All routes are prefixed with `/api` and are only reachable through the extension socket (or the
development TCP port).

| Method   | Path                                       | Description                                                  |
| -------- | ------------------------------------------ | ------------------------------------------------------------ |
| `GET`    | `/api/health`                              | backend + engine version/status                              |
| `GET`    | `/api/images`                              | local images (tags, id, size, created, usage)                |
| `DELETE` | `/api/images/{name}`                       | remove an image (`?force=true`)                              |
| `GET`    | `/api/networks`                            | networks (driver, subnet, containers, container count)       |
| `GET`    | `/api/volumes`                             | volumes (name, driver, mountpoint, created)                  |
| `GET`    | `/api/containers`                          | containers (state, ports, networks, mounts, restart policy)  |
| `POST`   | `/api/containers`                          | validate + create + start a container                        |
| `GET`    | `/api/containers/{id}/logs`                | logs (`?follow=true` for SSE, `?tail=N`)                     |
| `GET`    | `/api/containers/{id}/connections`         | TCP/UDP sockets in the container's network namespace         |
| `POST`   | `/api/containers/{id}/connections/kill`    | kill a connection (experimental)                             |
| `POST`   | `/api/containers/{id}/start`               | start                                                        |
| `POST`   | `/api/containers/{id}/restart`             | restart                                                      |
| `POST`   | `/api/containers/{id}/stop`                | stop (10 s timeout)                                          |
| `DELETE` | `/api/containers/{id}`                     | remove (`?force=true`, default)                              |
| `GET`    | `/api/resolve?ip=…`                        | resolve an IP to a container or host name                    |

### `POST /api/containers` payload

```jsonc
{
  "image": "nginx:latest",              // must exist locally, no surprise pulls
  "name": "my-nginx",                   // optional
  "command": "nginx -g 'daemon off;'",  // optional (shell-split)
  "entrypoint": "/docker-entrypoint.sh",// optional (shell-split)
  "hostname": "my-nginx",               // optional
  "restart_policy": "always",           // no | always | on-failure | unless-stopped

  "environment": { "TZ": "Europe/Berlin", "MODE": "prod" },

  "ports": [
    { "host_port": 8080, "container_port": 80, "host_ip": "127.0.0.1", "protocol": "tcp" },
    { "host_port": null, "container_port": 443, "protocol": "tcp" }   // random host port
  ],

  "mounts": [
    { "type": "bind",   "source": "/etc/nginx", "target": "/etc/nginx", "read_only": true },
    { "type": "volume", "source": "data-vol",   "target": "/var/lib/data" }
  ],

  "networks": ["bridge", "my-net"]      // first network at create, others attached after
}
```

The backend validates the image and every requested network before creating anything, and rejects
invalid payloads with `400`/`404` and a JSON `detail` message that the UI shows as a toast. Named
volumes in `mounts` are created on demand. Images are never pulled implicitly.

### Network connections

`GET /api/containers/{id}/connections` reads `/proc/net/{tcp,tcp6,udp,udp6}` from inside the
running (or paused) container via `docker exec`, decodes the socket tables and reports protocol,
address family, state, local/remote endpoints, UID, inode and queue sizes. Containers that share a
network namespace (for example `--network container:<id>` or host networking) are reported in
`netns.shared_with`.

`POST …/connections/kill` is **experimental**. It tries socket-level methods (`ss -K`, then a
short-lived `nicolaka/netshoot` helper container connected to the target container's network and
PID namespaces). The netshoot helper runs a comprehensive kill script trying multiple `ss -K` syntax
variations and `tcpkill` commands. Returns debug output when no method succeeds. Unlike container
creation, the netshoot helper may need to be pulled the first time it runs.

---

## Build & install

```bash
make build-extension     # docker build --tag imgfastler/imgfastler:latest .
make install-extension   # docker extension install imgfastler/imgfastler:latest
make update-extension    # after changing code
make inspect-extension   # what Docker Desktop knows about it
make uninstall-extension
```

Then open **Docker Desktop → Extensions** (restart Docker Desktop if the tab does not show up).

The backend container is a *system* container: enable **Settings → Show system containers** to watch
its logs in the Containers view while debugging.

---

## Local development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt

# 1. backend over plain TCP (bypasses the extension socket)
make run-backend                      # http://127.0.0.1:8099

# 2. frontend in a normal browser (falls back to the TCP backend automatically)
make dev-ui                           # http://127.0.0.1:8000
```

Run both at the same time, then open `http://127.0.0.1:8000`. The frontend connects to
`http://127.0.0.1:8099` by default; override it with the `?api=` query parameter:

```
http://127.0.0.1:8000/?api=http://127.0.0.1:8099
```

Useful targets:

```bash
make check            # syntax checks for backend, frontend and metafile
make test-backend     # end-to-end API smoke test (backend must be running)
make lint             # ruff check backend/
make format           # ruff format backend/
make lint-fix         # lint + format
```

The smoke test (`scripts/smoke.py`) walks the same flow as the UI: list images/networks, create a
container with environment variables, a port, a bind mount, a named volume and two networks, then
start/stop/remove it and verify the error paths.

### Alternative: run the backend in Docker

If the local venv cannot load compiled Python extensions (see [SETUP.md](SETUP.md)), run the
backend container instead:

```bash
# Terminal 1: backend in Docker (TEST_PORT=8099)
make run-backend-docker

# Terminal 2: frontend locally
make dev-ui

# Stop the backend
make stop-backend-docker
```

`docker-compose.dev.yaml` bind-mounts the host Docker socket and uses host networking so the API is
reachable on `127.0.0.1:8099`.

Inside Docker Desktop all calls go through `ddClient.extension.vm.service.*`, which Docker Desktop
proxies to the Unix socket declared in `metadata.json`.

---

## Configuration

| Variable                 | Default                                 | Purpose                                                        |
| ------------------------ | --------------------------------------- | -------------------------------------------------------------- |
| `EXTENSION_SOCKET`       | `/run/guest-services/imgfastler.sock`   | Unix socket the backend listens on (must match `metadata.json`) |
| `TEST_PORT`              | `0` (disabled)                          | Serve plain TCP on `127.0.0.1:<port>` instead of the socket     |
| `LOG_LEVEL`              | `INFO`                                  | Backend log level                                               |
| `IMGFASTLER_HELPER_IMAGE`| `${DESKTOP_PLUGIN_IMAGE}`               | Image used for the connection-inspection helper                 |

---

## Notes

* `metadata.json` (`ui.dashboard-tab.backend.socket` and `vm.exposes.socket`) must match the socket
  the backend listens on, and that name must match the socket path in `backend/server.py`
  (`EXTENSION_SOCKET` env var, default `/run/guest-services/imgfastler.sock`).
* `${DESKTOP_PLUGIN_IMAGE}` in `docker-compose.yaml` is substituted by Docker Desktop — never replace
  it by hand.
* The `com.docker.desktop.extension.icon` image label must be an absolute URL (it is shown in the
  Extensions marketplace); point it at your own hosted copy of `docker.svg` before publishing.
* The Dockerfile reuses the local `.venv` site-packages in the build stage because PyPI is not
  always reachable from the build environment. Rebuild the venv before building the image when
  dependencies change.
* Extension images are packaged as Docker images and distributed with semver tags; use
  `make push-extension` to publish a multi-arch (`linux/amd64`, `linux/arm64`) build.
