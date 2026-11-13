# agents.md

Guidance for AI agents working on this repository.

## What this is

**imgfastler** — a Docker Desktop extension for browsing local images and creating/managing containers. It has three parts:

- `ui/` — static frontend (plain HTML/CSS/JS, no build step). Served by Docker Desktop from `metadata.json` (`ui.dashboard-tab`). Talks to the backend via `ddClient.extension.vm.service.*`.
- `backend/` — a single-file FastAPI + docker-py service (`backend/server.py`) running as a container inside the Docker Desktop VM. Talks to the Engine via the VM's `/var/run/docker.sock.raw` socket, and serves `/api/*` over a Unix socket.
- `metadata.json` — the extension metafile; the socket name (`imgfastler.sock`) declared here must match the backend's `EXTENSION_SOCKET` env var (default `/run/guest-services/imgfastler.sock`).

## Commands

- `make run-backend` — backend locally on `http://127.0.0.1:8099` (needs a Docker socket; uses `.venv/bin/python`)
- `make dev-ui` — serve `ui/` on `http://127.0.0.1:8000` (falls back to the TCP backend automatically; override with `?api=`)
- `make run-backend-docker` / `make stop-backend-docker` — run the backend via `docker-compose.dev.yaml` if the local venv can't load compiled deps (see `SETUP.md`)
- `make test` — unit tests in `backend/tests` (no Docker daemon needed). Uses `.venv/bin/python` when it can import the backend deps, and automatically falls back to `~/.venvs/imgfastler/bin/python` otherwise (the `/mnt/projects` mount is `noexec`, so compiled extensions cannot load from `.venv` — see `SETUP.md`).
- `make test-backend` — end-to-end smoke test (`scripts/smoke.py`); requires a running backend on port 8099
- `make check` — syntax check `backend/server.py`, `ui/app.js` and `metadata.json`
- `make lint` / `make format` / `make lint-fix` — ruff on `backend/`
- `make build-extension` / `install-extension` / `update-extension` — build/install the extension into Docker Desktop

## Code structure

### `backend/server.py` (~1500 lines, single file)

One module, organized top-to-bottom in `# --- section ---` banner blocks:
1. **Configuration** — env vars (`EXTENSION_SOCKET`, `TEST_PORT`, `LOG_LEVEL`, `IMGFASTLER_HELPER_IMAGE`), default socket, logging setup.
2. **App** — FastAPI instance and cached docker-py client.
3. **Models** — Pydantic request models for container creation: `PortSpec`, `MountSpec`, `ContainerSpec` (with validators).
4. **Error helpers** — translate docker-py exceptions into HTTP errors with engine messages in the detail field.
5. **Helpers** — pure functions for port spec parsing/formatting, `/proc/net/*` socket decoding, self-container ID detection, helper-image lookup, and socket dumping.
6. **Routes** — all FastAPI endpoints under `/api/*`; thin wrappers that validate, call docker-py, and return plain dicts. `create_container` is the largest route, using port binding, volume existence checks, and network state helpers.
7. **Docker Hub documentation** — scrapes image READMEs and serves them via a dedicated endpoint.
8. **Entrypoint** — starts uvicorn on TCP if `TEST_PORT` is set; otherwise cleans stale sockets and runs uvicorn on the Unix socket.

### `ui/app.js` (~1700 lines, one IIFE)

Plain JS, no framework, no modules — everything inside a single `(() => { ... })()` with `'use strict'`. Sections marked by `/* ====== section ====== */` banners:

- **Constants** — view names (`VIEWS`), `DEFAULT_API_BASE` (`http://127.0.0.1:8099`), poll and toast intervals, `NAME_PATTERN`.
- **Plumbing** — `ddClient` detection, a mutable global `state` object (current view, cached lists, per-view UI state), `$`/`$$` shorthands, and `api(method, path, body)`: uses `ddClient.extension.vm.service.*` when inside Docker Desktop, otherwise `fetch` against `httpBase` (from the `?api=` query param).
- **Markdown rendering** — small hand-rolled renderer for image documentation (staged code blocks, paragraphs, blockquotes, lists, tables).
- **One `load*`/`render*` pair per view** — images, create form (env/port/mount dynamic rows), containers, networks, connections; plus `showView(name)` and the container create/edit form prefill.
- **Logs** — modal log viewer using SSE (`EventSource` on `?follow=true`) with tail and auto-scroll.
- **Event wiring at the bottom** — top-level `addEventListener` calls on `#nav`, table bodies (delegated clicks with `data-action` attributes), form submits, and the live-mode toggle.

### `ui/index.html` + `ui/styles.css`

Static markup: `#nav`, `#engine-info`, one `<section>` per view (`#view-images`, `#view-create`, `#view-containers`, `#view-networks`, `#view-connections`), and modals (logs, debug). Visibility is toggled by JS; `styles.css` is plain CSS, no preprocessor.

### `scripts/smoke.py`

Standalone end-to-end test (stdlib only, takes the base URL as argv): lists images/networks, creates a container with env, port, bind mount, volume and two networks, then starts/stops/removes it and checks the error paths.

### `backend/tests/`

Pytest unit tests (pytest + httpx, no Docker daemon needed — the docker-py client is faked in `test_create_container.py`). Run with `make test`. The `scripts/smoke.py` suite complements these as the real-Docker end-to-end check.

### Packaging

`Dockerfile` copies `ui/`, `backend/` and `metadata.json` into one image (reusing the local `.venv` site-packages); `docker-compose.yaml` is the runtime service (socket wiring), and `docker-compose.dev.yaml` is the standalone dev backend.

## Code style

- Backend: Python ≥ 3.12, ruff (see `.ruff.toml` and `pyproject.toml`), double quotes (`quote-style = "double"`).
- Frontend has no framework, no bundler, no linter — plain JS in a single IIFE in `ui/app.js`; keep it that way.
- Pre-commit runs ruff (`.pre-commit-config.yaml`).

## Gotchas

- Everything backend-side lives in `backend/server.py` (~1500 lines); helpers are plain module-level functions, FastAPI routes use `@app.get/post/delete` with `/api` prefix.
- Images are **never pulled implicitly**; validate the image and all requested networks before creating a container and return `400`/`404` with a JSON `detail` the UI can toast.
- The Dockerfile reuses the local `.venv` site-packages (PyPI may be unreachable at build time); rebuild the venv before building when dependencies change.
- Never edit `${DESKTOP_PLUGIN_IMAGE}` in `docker-compose.yaml` by hand — Docker Desktop substitutes it.
- The socket name must stay consistent across `metadata.json`, `docker-compose.yaml` and `backend/server.py` (`EXTENSION_SOCKET`).
- Use `docker.sock.raw` (VM daemon), not the host socket, for the extension backend.
