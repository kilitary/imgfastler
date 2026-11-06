# imgfastler — Docker Desktop extension

A typical Docker Desktop extension with all three extension components:

* **UI frontend** — static HTML/CSS/JS tab in the Docker Desktop dashboard that lists local images
* **Backend** — a Python (FastAPI + docker-py) service running inside the Desktop VM, speaking to the
  Docker Engine over the VM's socket
* **Metafile** — `metadata.json`, the entry point Docker Desktop reads to install the extension

Pick any image on the main page → **Run** → configure **mounts, networks, environment variables and
ports** → **Create container**.

---

## Layout

```
.
├── metadata.json           # metafile: dashboard tab + backend socket wiring
├── docker-compose.yaml     # backend service definition (adds the Docker socket)
├── Dockerfile              # packages ui/ + backend/ + metafile into one image
├── Makefile                # build / install / update / dev helpers
├── icon                    # docker.svg (image root, used by Docker Desktop)
├── scripts/
│   └── smoke.py            # end-to-end API smoke test
├── ui/                     # frontend — extracted and served by Docker Desktop
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   └── logo.svg
└── backend/                # backend — runs as a container in the Desktop VM
    ├── server.py
    └── requirements.txt
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

All routes are prefixed with `/api` and are only reachable through the extension socket.

| Method   | Path                          | Description                                             |
| -------- | ----------------------------- | ------------------------------------------------------- |
| `GET`    | `/api/health`                 | backend + engine version/status                          |
| `GET`    | `/api/images`                 | local images (tags, id, size, created, usage)            |
| `DELETE` | `/api/images/{name}`          | remove an image (`?force=true`)                          |
| `GET`    | `/api/networks`               | networks (driver, subnet, container count)               |
| `GET`    | `/api/volumes`                | volume names (for mount autocompletion)                  |
| `GET`    | `/api/containers`             | containers (state, ports, networks)                      |
| `POST`   | `/api/containers`             | validate + create + start a container                    |
| `POST`   | `/api/containers/{id}/start`  | start                                                    |
| `POST`   | `/api/containers/{id}/stop`   | stop                                                     |
| `DELETE` | `/api/containers/{id}`        | remove (`?force=true`)                                   |

### `POST /api/containers` payload

```jsonc
{
  "image": "nginx:latest",              // must exist locally, no surprise pulls
  "name": "my-nginx",                   // optional
  "command": "nginx -g 'daemon off;'",  // optional (shell-split)
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

Validation errors return `400`/`404` with a JSON `detail` message that the UI shows as a toast.

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

make check                            # syntax checks for backend, frontend and metafile
make test-backend                     # end-to-end API smoke test (backend must be running)
```

**Important**: Both services must be running simultaneously:
- Terminal 1: `make run-backend` (starts backend on port 8099)
- Terminal 2: `make dev-ui` (starts frontend on port 8000)

Then open `http://127.0.0.1:8000` in your browser. The frontend will automatically connect to the backend.

### Alternative: Run Backend in Docker

If you encounter the `ImportError: failed to map segment from shared object` error (common in sandboxed environments), run the backend in Docker instead:

```bash
# Terminal 1: Run backend in Docker
make run-backend-docker

# Terminal 2: Run frontend locally
make dev-ui
```

Stop the Docker backend with:
```bash
make stop-backend-docker
```

The `?api=` query parameter can override the backend URL:
```
http://127.0.0.1:8000/?api=http://127.0.0.1:8099
```

Inside Docker Desktop all calls go through `ddClient.extension.vm.service.*`, which Docker Desktop
proxies to the Unix socket declared in `metadata.json`.

---

## Notes

* `metadata.json` (`ui.dashboard-tab.backend.socket` and `vm.exposes.socket`) must match the socket
  the backend listens on, and that name must match the socket path in `backend/server.py`
  (`EXTENSION_SOCKET` env var, default `/run/guest-services/imgfastler.sock`).
* `${DESKTOP_PLUGIN_IMAGE}` in `docker-compose.yaml` is substituted by Docker Desktop — never replace
  it by hand.
* The `com.docker.desktop.extension.icon` image label must be an absolute URL (it is shown in the
  Extensions marketplace); point it at your own hosted copy of `docker.svg` before publishing.
* Extension images are packaged as Docker images and distributed with semver tags; use
  `make push-extension` to publish.
