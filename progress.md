# Progress Log

Use this file as the chronological record of work performed, files changed, validation results, and errors.

## Session: 2026-11-26

### Phase 1: Setup

- **Status:** complete
- **Started:** 2026-11-26
- Actions taken:
  - Read the imgfastler project (backend/server.py, ui/app.js, ui/index.html, ui/styles.css, Makefile, README.md, SETUP.md, AGENTS.md, metadata.json, docker-compose.yaml, Dockerfile, scripts/, tests/, grafana/, collector/).
  - Initialized planning files at the project root (task_plan.md, findings.md, progress.md) from the planning-with-files skill templates.
  - Recorded git status: ~50 modified files in the working tree; planning files are new.
- Files created/modified:
  - task_plan.md (new)
  - findings.md (new)
  - progress.md (new)

### Phase 2: Populate Planning Files

- **Status:** complete
- Actions taken:
  - Populated findings.md with project overview, architecture, and dev-environment notes.
- Files created/modified:
  - findings.md (updated)

### Makefile: `dev-tcp` target (2026-11-26)

- Added `dev-tcp` to `Makefile` (after `dev-ui`): runs `make run-backend &` (backend in test TCP mode on `http://127.0.0.1:$(TEST_PORT)`) plus `make dev-ui` (UI on `http://127.0.0.1:8000`).
- Added `dev-tcp` to `.PHONY`.
- Verified: `make -n dev-tcp` shows both recipes; `make -s help` lists the new target.
- Files created/modified:
  - Makefile (updated)

### Fix: network setup on container edit (bridge -> host) (2026-11-26)

- **Status:** complete
- Actions taken:
  - Traced edit flow: `openEdit()` prefills form, calls `showView('create')` which triggers `loadNetworks()`; `renderNetworks()` rebuilt `#network-list` and always checked `bridge`, clobbering edit prefill. `collectSpec()`/`submitCreate()` (stop+remove+POST `/api/containers`) were otherwise correct for edit.
  - Backend `create_container()` validated networks exist but allowed `host` combined with other networks, ports, or hostname, causing Docker engine errors.
  - UI fix: added `state.editNetworks`, made `renderNetworks(selected)` preserve DOM selection / fallback to `editNetworks` / default `bridge` only for fresh create; `openEdit()` stores container networks and renders when cached; `resetCreateForm()` clears editNetworks; network change handler clears editNetworks, enforces host/none exclusivity, and clears hostname/port rows on host/none selection; `collectSpec()` validates host constraints.
  - Backend fix: `POST /api/containers` returns 400 when `host` is combined with other networks, port mappings, or hostname.
  - Added unit tests: `test_host_network_rejected_with_other_networks`, `test_host_network_rejected_with_ports`, `test_host_network_rejected_with_hostname`, `test_create_with_host_network`.
- Files created/modified:
  - ui/app.js (updated)
  - backend/server.py (updated)
  - backend/tests/test_create_container.py (updated)
  - task_plan.md, findings.md, progress.md (updated)

### Feature: process name and PID in connections table (2026-11-26)

- **Status:** complete
- Actions taken:
  - Extended `_dump_container_sockets()` to emit `__IMFASTLER_procs__` section mapping socket inodes to `pid\tcomm` from `/proc/[pid]/fd`.
  - Added `_parse_proc_inode_map()` and enriched `/api/containers/{id}/connections` results with `pid` (int|null) and `process` (str).
  - Updated UI connections table: added PID and Process columns, updated colspans, search haystack, placeholder, and row rendering.
  - Bumped version to 0.1.0 in `pyproject.toml` and FastAPI app.
  - Updated README connections description.
- Files created/modified:
  - backend/server.py (updated)
  - ui/app.js (updated)
  - ui/index.html (updated)
  - pyproject.toml (updated)
  - README.md (updated)
  - task_plan.md, progress.md (updated)

## Test Results

| Test | Input | Expected | Actual | Status |
|------|-------|----------|--------|--------|
| pytest backend/tests/test_create_container.py | host network cases | 4 new tests pass, no regressions | 27 passed, 1 warning | pass |
| make check | syntax | ok | py_compile server.py, metadata.json OK | pass |
| node --check ui/app.js | syntax | ok | OK | pass |
| ruff check backend/server.py backend/tests/test_create_container.py | lint changed files | no new errors | 6 pre-existing errors in server.py (unchanged lines) | pass (no regressions) |

## Error Log

| Timestamp | Error | Attempt | Resolution |
|-----------|-------|---------|------------|
|           |       | 1       |            |

## 5-Question Reboot Check

| Question | Answer |
|----------|--------|
| Where am I? | Connections table now includes PID and process name |
| Where am I going? | Await user verification / follow-up |
| What's the goal? | Add process name and process ID to connections table |
| What have I learned? | Socket inode->process mapping can be collected via `/proc/[pid]/fd` readlink scan in container exec; expose as extra dump section for parsing |
| What have I done? | Backend dump/parser/enrichment, UI table/cols/search/render, README update, version bump to 0.1.0, syntax checks pass |

---

*Update this file after completing a phase, running validation, or encountering an error.*
