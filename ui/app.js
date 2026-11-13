/* ==========================================================================
   imgfastler — Docker Desktop extension frontend (vanilla JS)
   --------------------------------------------------------------------------
   Talks to the Python backend through the Docker Desktop extension SDK:

       window.ddClient.extension.vm.service.get/post/delete(...)

   Docker Desktop proxies those calls to the Unix socket declared in
   metadata.json (`vm.exposes.socket`). When the file is opened outside
   Docker Desktop the code falls back to plain HTTP (TEST_PORT mode).
   ========================================================================== */

(() => {
  'use strict';

  /* ============================== constants =============================== */

  const VIEWS = ['images', 'create', 'containers', 'networks', 'connections'];
  const DEFAULT_API_BASE = 'http://127.0.0.1:8099';
  const LIVE_POLL_INTERVAL = 5000;
  const LOGS_POLL_INTERVAL = 2000;
  const CONFIRM_TIMEOUT = 3000;
  const TOAST_DURATION = 5000;
  const NAME_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/;

  /* ============================== plumbing =============================== */

  const dd = window.ddClient;
  const vmService = dd && dd.extension && dd.extension.vm ? dd.extension.vm.service : null;
  const httpBase =
    new URLSearchParams(window.location.search).get('api') || DEFAULT_API_BASE;

  const state = {
    view: 'images',
    images: [],
    containers: [],
    networks: [],
    networksList: [],
    volumes: [],
    image: null,
    editContainerId: null,
    connectionsContainerId: null,
    connectionsContainer: null,
    connectionsData: null,
    connectionsResolveIPs: true,
    connectionsResolvedIPs: {},
    connectionsResolvingIPs: false,
    busy: false,
    live: false,
    liveTimer: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  async function api(method, path, body) {
    if (vmService && typeof vmService[method] === 'function') {
      return method === 'get' ? await vmService.get(path) : await vmService[method](path, body);
    }

    const response = await fetch(httpBase + path, {
      method: method.toUpperCase(),
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    if (!response.ok) {
      const error = new Error(_extractDetailMessage(data, `${response.status} ${text}`));
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function _extractDetailMessage(detail, fallback) {
    let value = detail;
    if (detail && typeof detail === 'object' && !Array.isArray(detail) && detail.detail !== undefined) {
      value = detail.detail;
    }
    const messageFrom = (item) => {
      if (!item || typeof item !== 'object') return null;
      const loc = Array.isArray(item.loc) ? item.loc.filter(Boolean).join('.') : item.loc;
      const msg = item.msg || item.message || item.type;
      if (!msg) return null;
      return loc ? `${loc}: ${msg}` : String(msg);
    };
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) {
      const messages = value.map(messageFrom).filter(Boolean);
      if (messages.length) return messages.join(' ');
      return fallback || null;
    }
    if (value && typeof value === 'object') {
      const nested = value.detail || value.message || value.error || value.msg;
      if (typeof nested === 'string') return nested;
      if (Array.isArray(nested)) return _extractDetailMessage(nested, fallback);
      if (nested && typeof nested === 'object') return messageFrom(nested) || fallback;
    }
    return fallback || null;
  }

  function errorMessage(error) {
    if (!error) return 'Unknown error';
    if (typeof error === 'string') return error;

    const candidates = [
      error.detail,
      typeof error.error === 'string' ? error.error : error.error && error.error.message,
      error.message,
      error.body && (error.body.detail || error.body.message),
      error.response && error.response.data,
    ];
    for (const candidate of candidates) {
      let message = null;
      if (typeof candidate === 'string') {
        message = candidate;
      } else if (Array.isArray(candidate)) {
        message = _extractDetailMessage(candidate);
      } else if (candidate && typeof candidate === 'object') {
        const nested = candidate.detail || candidate.message || candidate.error;
        message = typeof nested === 'string' ? nested : _extractDetailMessage(nested);
      }
      if (message && String(message).trim()) {
        return _cleanMessage(message);
      }
    }
    try {
      return _cleanMessage(JSON.stringify(error));
    } catch {
      return String(error);
    }
  }

  const _cleanMessage = (message) =>
    message
      .replace(/^Error invoking remote method '[^']+':\s*/, '')
      .replace(/^Error:\s*/, '')
      .trim();

  /* ============================== helpers =============================== */

  const HTML_ESCAPE_MAP = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  };

  const escapeHtml = (value) =>
    String(value === null || value === undefined ? '' : value).replace(
      /[&<>"']/g,
      (character) => HTML_ESCAPE_MAP[character],
    );

  /* --------------------------- markdown rendering --------------------------- */

  // Minimal, hardened Markdown renderer for Docker Hub README content.
  // All input is HTML-escaped first; links/images only accept http(s) URLs.
  function renderMarkdown(source) {
    const text = String(source === null || source === undefined ? '' : source)
      .replace(/\r\n?/g, '\n');

    // Pull fenced code blocks out first so their content is never parsed.
    const codeBlocks = [];
    const staged = text.replace(/```[^\n]*\n?([\s\S]*?)```/g, (match, code) => {
      codeBlocks.push(`<pre class="docs-code"><code>${escapeHtml(code.replace(/\n$/, ''))}</code></pre>`);
      return `\n\u0000${codeBlocks.length - 1}\u0000\n`;
    });

    const html = [];
    let paragraph = [];
    let blockquote = [];
    let list = null;      // { tag: 'ul' | 'ol' }
    let listOpen = false; // an <li> is currently open
    let table = null;     // { head: string[], rows: string[][] }

    const inline = (value) => formatDocsInline(value);

    const flushParagraph = () => {
      if (paragraph.length) {
        html.push(`<p>${inline(paragraph.join(' '))}</p>`);
        paragraph = [];
      }
    };
    const flushBlockquote = () => {
      if (blockquote.length) {
        html.push(`<blockquote>${blockquote.map((line) => `<p>${inline(line)}</p>`).join('')}</blockquote>`);
        blockquote = [];
      }
    };
    const flushList = () => {
      if (list) {
        if (listOpen) html.push('</li>');
        html.push(`</${list.tag}>`);
        list = null;
        listOpen = false;
      }
    };
    const flushTable = () => {
      if (!table) return;
      const head = table.head;
      const body = table.rows
        .map((cells) => `<tr>${head.map((cell, index) => `<td>${inline(cells[index] || '')}</td>`).join('')}</tr>`)
        .join('');
      html.push(
        `<table><thead><tr>${head.map((cell) => `<th>${inline(cell)}</th>`).join('')}</tr></thead>` +
        (body ? `<tbody>${body}</tbody>` : '') +
        '</table>',
      );
      table = null;
    };
    const flush = () => {
      flushParagraph();
      flushBlockquote();
      flushList();
      flushTable();
    };

    for (const rawLine of staged.split('\n')) {
      const line = rawLine.trimEnd();
      const codeRef = line.match(/^\u0000(\d+)\u0000$/);

      if (codeRef) {
        flush();
        html.push(codeBlocks[Number(codeRef[1])]);
        continue;
      }
      if (!line.trim()) {
        flush();
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      if (heading) {
        flush();
        const level = heading[1].length;
        html.push(`<h${level}>${inline(heading[2].replace(/#+\s*$/, ''))}</h${level}>`);
        continue;
      }

      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        flush();
        html.push('<hr>');
        continue;
      }

      const quote = line.match(/^>\s?(.*)$/);
      if (quote) {
        flushParagraph();
        flushList();
        flushTable();
        blockquote.push(quote[1]);
        continue;
      }

      const listItem = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
      if (listItem) {
        flushParagraph();
        flushBlockquote();
        flushTable();
        const tag = /[-*+]/.test(listItem[2]) ? 'ul' : 'ol';
        if (!list || list.tag !== tag) {
          flushList();
          list = { tag };
          html.push(`<${tag}>`);
        }
        if (listOpen) html.push('</li>');
        html.push(`<li>${inline(listItem[3])}`);
        listOpen = true;
        continue;
      }

      if (/^\s*\|.*\|\s*$/.test(line)) {
        flushParagraph();
        flushBlockquote();
        flushList();
        const cells = line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
        const isSeparator = cells.every((cell) => cell === '' || /^:?-{3,}:?$/.test(cell));
        if (!table) {
          table = { head: cells, rows: [] };
        } else if (!isSeparator) {
          table.rows.push(cells);
        } else if (!table.rows.length && !table.head.length) {
          table = null; // orphan separator without a header row
        }
        continue;
      }

      flushBlockquote();
      flushList();
      flushTable();
      paragraph.push(line.trim());
    }

    flush();
    return html.join('\n');
  }

  function formatDocsInline(value) {
    let parsed = escapeHtml(String(value === null || value === undefined ? '' : value));
    // Images ![alt](https://...)
    parsed = parsed.replace(
      /!\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g,
      (match, alt, url) =>
        `<img src="${url}" alt="${alt || ''}" loading="lazy" decoding="async">`,
    );
    // Links [text](https://...)
    parsed = parsed.replace(
      /\[([^\]]*)\]\((https?:\/\/[^)\s]+)\)/g,
      (match, label, url) =>
        `<a href="${url}" target="_blank" rel="noopener noreferrer">${label || url}</a>`,
    );
    // Bare URLs (only outside existing href="..." attributes)
    parsed = parsed.replace(
      /(^|[\s(>])(https?:\/\/[^\s)<]+)/g,
      (match, prefix, url) =>
        `${prefix}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`,
    );
    // Inline code, then bold/italic.
    parsed = parsed.replace(/`([^`]+)`/g, (match, code) => `<code>${code}</code>`);
    parsed = parsed.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    parsed = parsed.replace(/(^|\W)\*([^*]+)\*(?=\W|$)/g, '$1<em>$2</em>');
    return parsed;
  }

  function formatBytes(bytes) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let value = Number(bytes) || 0;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)}\u00A0${units[unit]}`;
  }

  function timeAgo(epochSeconds) {
    if (!epochSeconds) return '—';
    const seconds = Math.max(0, Math.floor(Date.now() / 1000) - epochSeconds);
    const units = [
      ['y', 31536000],
      ['mo', 2592000],
      ['d', 86400],
      ['h', 3600],
      ['m', 60],
    ];
    for (const [suffix, size] of units) {
      if (seconds >= size) return `${Math.floor(seconds / size)}${suffix} ago`;
    }
    return 'just now';
  }

  let toastTimer;
  function toast(type, message) {
    const text = String(message);
    if (dd && dd.desktopUI && dd.desktopUI.toast && dd.desktopUI.toast[type]) {
      dd.desktopUI.toast[type](text);
      return;
    }
    const element = $('#toast');
    element.textContent = text;
    element.className = `toast is-${type}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => element.classList.add('hidden'), TOAST_DURATION);
  }

  const showError = (error) => toast('error', errorMessage(error));

  function setBusy(busy) {
    state.busy = busy;
    $('#submit-btn').disabled = busy;
    $('#submit-btn').textContent = busy ? 'Creating…' : 'Create container';
  }

  /* ============================== views =============================== */

  function showView(name) {
    state.view = name;
    VIEWS.forEach((view) => $(`#view-${view}`).classList.toggle('hidden', view !== name));
    // Connections is a sub-view of Containers, keep that tab highlighted.
    const navView = name === 'connections' ? 'containers' : name;
    $$('.nav-btn').forEach((button) =>
      button.classList.toggle('is-active', button.dataset.view === navView),
    );

    if (name === 'images' && !state.images.length) loadImages();
    if (name === 'containers') loadContainers();
    if (name === 'networks') loadNetworksList();
    if (name === 'connections') loadConnections();
    if (name === 'create') {
      loadNetworks();
      loadVolumes();
    }
  }

  /* ------------------------------- images ------------------------------ */

  function imageLabel(image) {
    return image.tags.length ? image.tags[0] : `<none> ${image.short_id}`;
  }

  // What the API accepts as an image reference (untagged images use their id).
  const imageRef = (image) => (image.tags.length ? image.tags[0] : image.id);

  async function loadImages() {
    const body = $('#images-body');
    body.innerHTML = '<tr class="row-loading"><td colspan="6">Loading images…</td></tr>';
    $('#images-error').classList.add('hidden');

    try {
      state.images = await api('get', '/api/images');
    } catch (error) {
      $('#images-empty').classList.add('hidden');
      const panel = $('#images-error');
      const errorMsg = errorMessage(error);
      panel.textContent = errorMsg;
      panel.classList.remove('hidden');
      body.innerHTML = '';

      // Add helpful hint if backend is not available
      if (errorMsg.includes('Failed to fetch') || errorMsg.includes('NetworkError')) {
        panel.innerHTML += '<br><small class="muted">Backend not running. Start with: make run-backend</small>';
      }
      return;
    }
    renderImages();
    markUpdated();
  }

  function renderImages() {
    const query = $('#image-search').value.trim().toLowerCase();
    const images = state.images.filter((image) =>
      !query ? true : `${image.tags.join(' ')} ${image.id} ${image.short_id}`.toLowerCase().includes(query),
    );

    $('#image-count').textContent = `${images.length} of ${state.images.length} images`;
    $('#images-empty').classList.toggle('hidden', images.length > 0);

    $('#images-body').innerHTML = images
      .map((image) => {
        const tags = image.tags.length
          ? image.tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join('')
          : '<span class="tag">&lt;none&gt;</span>';
        return `
          <tr data-image="${escapeHtml(imageLabel(image))}">
            <td><div class="tags">${tags}</div></td>
            <td class="mono">${escapeHtml(image.short_id)}</td>
            <td>${formatBytes(image.size)}</td>
            <td>${timeAgo(image.created)}</td>
            <td>${image.in_use > 0 ? `${image.in_use} container(s)` : '<span class="muted">—</span>'}</td>
            <td class="col-actions">
              <button class="btn btn-sm btn-primary" data-action="run" type="button">Run</button>
              <button class="btn btn-sm" data-action="layers" type="button">Layers</button>
              <button class="btn btn-sm btn-danger" data-action="remove-image" type="button">Delete</button>
            </td>
          </tr>`;
      })
      .join('');
  }

  function openCreate(image) {
    state.image = image;
    resetCreateForm();
    $('#create-image').textContent = imageLabel(image);
    showView('create');
  }

  // Pre-populate the create form with a container's current parameters so the
  // user can edit them and submit to recreate the container.
  function openEdit(container) {
    // Match container's image reference against available local images.
    const containerImageRef = container.image || '';
    const image = state.images.find((img) =>
      img.tags.length ? img.tags.some((tag) => tag === containerImageRef || tag.startsWith(containerImageRef + ':')) : false
    );
    state.image = image || null;
    state.editContainerId = container.id;
    resetCreateForm();
    $('#create-image').textContent = `Edit — ${escapeHtml(container.name || container.short_id)}`;
    $('#c-name').value = container.name || '';
    $('#c-command').value = container.command || '';
    $('#c-entrypoint').value = container.entrypoint || '';
    $('#c-hostname').value = container.hostname || '';
    $('#c-restart').value = container.restart_policy || 'no';

    // Environment variables: Docker returns ["KEY=value", ...]
    $$('#env-rows .row').forEach((row) => row.remove());
    (container.environment || []).forEach((env) => {
      const eq = env.indexOf('=');
      if (eq === -1) return;
      addEnvRow(env.slice(0, eq), env.slice(eq + 1));
    });
    if (!container.environment || !container.environment.length) addEnvRow();

    // Ports
    $$('#port-rows .row').forEach((row) => row.remove());
    (container.ports_structured || []).forEach((port) => {
      addPortRow(
        port.host_port || '',
        port.container_port || '',
        port.protocol || 'tcp',
        port.host_ip || ''
      );
    });
    if (!container.ports_structured || !container.ports_structured.length) addPortRow();

    // Mounts
    $$('#mount-rows .row').forEach((row) => row.remove());
    // Prepend volume mount sources that aren't in the fetched volume list
    // so they still appear as selectable options when editing.
    const knownVolumeNames = new Set((state.volumes || []).map((v) => v.name));
    (container.mounts || [])
      .filter((m) => (m.type || '').toLowerCase() === 'volume' && m.source && !knownVolumeNames.has(m.source))
      .forEach((m) => state.volumes.unshift({ name: m.source }));
    (container.mounts || []).forEach((mount) => {
      addMountRow(mount.type, mount.source, mount.target, mount.read_only);
    });
    if (!container.mounts || !container.mounts.length) addMountRow();

    // Networks: check the ones the container is attached to
    $$('#network-list input:checked').forEach((input) => {
      input.checked = false;
    });
    (container.networks || []).forEach((network) => {
      const input = $(`#network-list input[value="${escapeHtml(network)}"]`);
      if (input) input.checked = true;
    });

    showView('create');
  }

  async function deleteImage(image, button) {
    if (button.dataset.armed === '1') {
      delete button.dataset.armed;
      button.textContent = 'Delete';
      try {
        await api('delete', `/api/images/${encodeURIComponent(imageRef(image))}?force=true`);
        state.images = state.images.filter((item) => imageLabel(item) !== imageLabel(image));
        toast('success', `Image '${imageLabel(image)}' removed`);
        await loadImages();
      } catch (error) {
        if (button) delete button.dataset.armed;
        showError(error);
      }
      return;
    }
    button.dataset.armed = '1';
    button.textContent = 'Sure?';
    setTimeout(() => {
      if (button.dataset.armed === '1') button.textContent = 'Delete';
    }, CONFIRM_TIMEOUT);
  }

  /* ------------------------------- images UI --------------------------- */

  function resetCreateForm() {
    state.editContainerId = null;
    $('#create-form').reset();
    $('#env-rows').innerHTML = '';
    $('#port-rows').innerHTML = '';
    $('#mount-rows').innerHTML = '';
    $('#create-status').textContent = '';
    setBusy(false);
    addEnvRow();
    addPortRow();
    addMountRow();
  }

  /* ------------------------- dynamic form rows ------------------------- */

  function removeButton(row) {
    row.querySelector('.row-remove').addEventListener('click', () => row.remove());
  }

  function addEnvRow(key = '', value = '') {
    const row = document.createElement('div');
    row.className = 'row env';
    row.innerHTML = `
      <input class="env-key mono" type="text" placeholder="KEY" aria-label="Variable name" autocomplete="off" spellcheck="false" value="${escapeHtml(key)}" />
      <input class="env-value mono" type="text" placeholder="value" aria-label="Variable value" autocomplete="off" spellcheck="false" value="${escapeHtml(value)}" />
      <button class="row-remove" type="button" title="Remove variable" aria-label="Remove variable">×</button>`;
    removeButton(row);
    $('#env-rows').appendChild(row);
    row.querySelector('.env-key').focus();
  }

  function addPortRow(hostPort = '', containerPort = '', protocol = 'tcp', hostIp = '') {
    const row = document.createElement('div');
    row.className = 'row port';
    row.innerHTML = `
      <input class="port-host" type="number" min="1" max="65535" placeholder="Host port" aria-label="Host port" autocomplete="off" value="${escapeHtml(hostPort)}" />
      <input class="port-container" type="number" min="1" max="65535" placeholder="Container port" aria-label="Container port" autocomplete="off" value="${escapeHtml(containerPort)}" />
      <select class="port-protocol" aria-label="Protocol">
        <option value="tcp"${protocol === 'tcp' ? ' selected' : ''}>tcp</option>
        <option value="udp"${protocol === 'udp' ? ' selected' : ''}>udp</option>
      </select>
      <input class="port-ip mono" type="text" placeholder="0.0.0.0" aria-label="Host IP" autocomplete="off" spellcheck="false" value="${escapeHtml(hostIp)}" />
      <button class="row-remove" type="button" title="Remove port" aria-label="Remove port">×</button>`;
    removeButton(row);
    $('#port-rows').appendChild(row);
    if (!containerPort) row.querySelector('.port-container').focus();
  }

  function addMountRow(type = 'bind', source = '', target = '', readOnly = false) {
    const row = document.createElement('div');
    row.className = 'row mount';
    row.innerHTML = `
      <select class="mount-type" aria-label="Mount type">
        <option value="bind"${(type || '').toLowerCase() === 'bind' ? ' selected' : ''}>bind</option>
        <option value="volume"${(type || '').toLowerCase() === 'volume' ? ' selected' : ''}>volume</option>
      </select>
      <div class="mount-source-slot"></div>
      <input class="mount-target mono" type="text" placeholder="/container/path" aria-label="Mount target" autocomplete="off" value="${escapeHtml(target)}" />
      <label><input class="mount-readonly" type="checkbox" aria-label="Read only mount" /> read-only</label>
      <button class="row-remove" type="button" title="Remove mount" aria-label="Remove mount">×</button>`;

    // Set the checkbox checked property directly after inserting HTML
    const readonlyCheckbox = row.querySelector('.mount-readonly');
    if (readonlyCheckbox) {
      readonlyCheckbox.checked = readOnly;
    }

    const typeSelect = row.querySelector('.mount-type');
    const slot = row.querySelector('.mount-source-slot');
    const isVolume = (type || '').toLowerCase() === 'volume';

    const volumeOptionsHtml = () => {
      const existing = state.volumes
        .map((volume) => `<option value="${escapeHtml(volume.name)}">${escapeHtml(volume.name)}</option>`)
        .join('');
      return existing
        ? `${existing}<option value="__new__">New volume…</option>`
        : '<option value="__new__">New volume…</option><option disabled>No volumes</option>';
    };

    // Render exactly one source control in the slot:
    // 'bind' → text input, 'volume' → dropdown of existing volumes,
    // 'new' → text input for a volume name (the dropdown mutates into this).
    function renderSource(kind, value = '', focus = false) {
      let control;
      if (kind === 'volume') {
        const select = document.createElement('select');
        select.className = 'mount-source volume-select';
        select.setAttribute('aria-label', 'Mount source');
        select.innerHTML = volumeOptionsHtml();
        if (value) {
          select.value = value;
          // Prefilled volume that is not in the list → let the user type it.
          if (select.value !== value) {
            renderSource('new', value, focus);
            return;
          }
        }
        select.addEventListener('change', () => {
          if (select.value === '__new__') {
            renderSource('new', '', true);
          }
        });
        control = select;
      } else if (kind === 'new') {
        const input = document.createElement('input');
        input.className = 'mount-source-new mono';
        input.type = 'text';
        input.placeholder = 'new volume name';
        input.setAttribute('aria-label', 'New volume name');
        input.autocomplete = 'off';
        input.spellcheck = false;
        input.value = value;
        // Leave the field empty and blurred to get the volume list back.
        input.addEventListener('blur', () => {
          if (!input.value) renderSource('volume', '', false);
        });
        input.addEventListener('keydown', (event) => {
          if (event.key === 'Escape' && !input.value) renderSource('volume', '', false);
        });
        control = input;
      } else {
        const input = document.createElement('input');
        input.className = 'mount-source mono';
        input.type = 'text';
        input.placeholder = '/host/path';
        input.setAttribute('aria-label', 'Mount source');
        input.autocomplete = 'off';
        input.value = value;
        control = input;
      }
      slot.innerHTML = '';
      slot.appendChild(control);
      if (focus) control.focus();
    }

    typeSelect.addEventListener('change', () => {
      renderSource(typeSelect.value === 'volume' ? 'volume' : 'bind', '', false);
    });

    removeButton(row);
    $('#mount-rows').appendChild(row);
    renderSource(isVolume ? 'volume' : 'bind', source, !source);
  }

  async function loadNetworks() {
    const container = $('#network-list');
    if (state.networks.length) {
      renderNetworks();
    } else {
      container.innerHTML = '<span class="muted">Loading networks…</span>';
      try {
        state.networks = await api('get', '/api/networks');
        renderNetworks();
      } catch (error) {
        container.innerHTML = `<span class="muted">Could not list networks: ${escapeHtml(
          errorMessage(error),
        )}</span>`;
      }
    }
    await loadNetworkUsage();
  }

  async function loadNetworkUsage() {
    const container = $('#create-network-usage');
    if (!container) return;
    container.innerHTML = '<span class="muted">Loading…</span>';
    try {
      const containers = await api('get', '/api/containers');
      state.containers = containers;
      const byNetwork = new Map();
      containers.forEach((item) => {
        (item.networks || []).forEach((network) => {
          if (!byNetwork.has(network)) byNetwork.set(network, []);
          byNetwork.get(network).push(item);
        });
      });
      const networks = state.networks.length ? state.networks : await api('get', '/api/networks');
      if (!state.networks.length) state.networks = networks;
      const rows = networks.map((network) => {
        const connected = byNetwork.get(network.name) || [];
        const meta = [network.driver, network.subnet].filter(Boolean).join(' · ');
        const containerText = connected.length
          ? connected.map((c) => `${escapeHtml(c.name)} (${escapeHtml(c.state)})`).join(', ')
          : '<span class="muted">no containers</span>';
        return `<div class="network-usage-row">
          <div class="cell-title">${escapeHtml(network.name)} <span class="muted">${escapeHtml(meta)}</span></div>
          <div class="network-usage-containers">${containerText}</div>
        </div>`;
      });
      container.innerHTML = rows.length ? rows.join('') : '<span class="muted">No networks found.</span>';
      const summary = $('#network-usage-summary');
      if (summary) {
        summary.textContent = `${networks.length} network(s) — expand to see which containers use them`;
      }
    } catch (error) {
      container.innerHTML = `<span class="muted">Could not load network usage: ${escapeHtml(
        errorMessage(error),
      )}</span>`;
      const summary = $('#network-usage-summary');
      if (summary) summary.textContent = 'Network usage — unavailable';
    }
  }

  async function loadVolumes() {
    try {
      state.volumes = await api('get', '/api/volumes');
    } catch (error) {
      state.volumes = [];
    }
  }

  function renderNetworks() {
    $('#network-list').innerHTML = state.networks
      .map((network) => {
        const meta = [network.driver, network.subnet].filter(Boolean).join(' · ');
        return `
          <label class="chip">
            <input type="checkbox" value="${escapeHtml(network.name)}"${
              network.name === 'bridge' ? ' checked' : ''
            } />
            <span>${escapeHtml(network.name)}</span>
            <span class="chip-meta">${escapeHtml(meta)}</span>
          </label>`;
      })
      .join('');
  }

  /* ============================ networks view ========================== */

  async function loadNetworksList() {
    const body = $('#networks-body');
    body.innerHTML =
      '<tr class="row-loading"><td colspan="5">Loading networks…</td></tr>';
    $('#networks-error').classList.add('hidden');

    try {
      state.networksList = await api('get', '/api/networks');
      renderNetworksList();
    } catch (error) {
      $('#networks-error').classList.remove('hidden');
      $('#networks-error').textContent = `Could not load networks: ${escapeHtml(
        errorMessage(error),
      )}`;
    }
  }

  function renderNetworksList() {
    const query = $('#network-search').value.trim().toLowerCase();
    const networks = state.networksList.filter((network) => {
      if (!query) return true;
      const haystack = `${network.name} ${network.driver} ${network.subnet} ${
        network.ipv6_subnet || ''
      } ${network.containers
        .map((c) => `${c.name} ${c.ipv4_address} ${c.ipv6_address}`)
        .join(' ')}`.toLowerCase();
      return haystack.includes(query);
    });

    $('#network-count').textContent = `${networks.length} of ${state.networksList.length} network${
      state.networksList.length === 1 ? '' : 's'
    }`;
    $('#networks-empty').classList.toggle('hidden', networks.length > 0);

    $('#networks-body').innerHTML = networks
      .map((network) => {
        const hasContainers = network.containers.length > 0;
        const networkId = network.name.replace(/[^a-zA-Z0-9]/g, '-');
        const networkRow = `
          <tr class="network-row" data-network="${escapeHtml(network.name)}">
            <td>
              <span class="cell-title">${escapeHtml(network.name)}</span>
              <span class="cell-sub mono">${escapeHtml(network.id.slice(0, 12))}</span>
            </td>
            <td>${escapeHtml(network.driver)}</td>
            <td>${escapeHtml(network.subnet || '—')}</td>
            <td>${escapeHtml(network.ipv6_subnet || '—')}</td>
            <td class="col-actions">
              <span class="badge ${network.container_count ? 'is-running' : 'is-other'}">
                ${network.container_count}
              </span>
              ${hasContainers ? `<button class="btn btn-sm btn-ghost" data-action="toggle-containers" data-network-id="${networkId}" type="button">Show containers</button>` : ''}
            </td>
          </tr>`;

        const containerRows = network.containers
          .map(
            (c) => `
            <tr class="network-container-row hidden" data-network-id="${networkId}">
              <td colspan="2" class="cell-title">${escapeHtml(c.name)}</td>
              <td>${escapeHtml(c.ipv4_address || '—')}</td>
              <td>${escapeHtml(c.ipv6_address || '—')}</td>
              <td class="mono">${escapeHtml(c.id.slice(0, 12))}</td>
            </tr>`,
          )
          .join('');

        return networkRow + containerRows;
      })
      .join('');

    // Toggle container visibility
    $$('#networks-body [data-action="toggle-containers"]').forEach((button) => {
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        const networkId = button.dataset.networkId;
        console.log('Toggle containers for network:', networkId);
        const containerRows = document.querySelectorAll(
          `#networks-body .network-container-row[data-network-id="${networkId}"]`
        );
        console.log('Found container rows:', containerRows.length);
        
        // Toggle visibility
        const firstRow = containerRows[0];
        const showing = firstRow && !firstRow.classList.contains('hidden');
        console.log('Currently showing:', showing);
        containerRows.forEach((containerRow) => {
          containerRow.classList.toggle('hidden', showing);
        });
        
        button.textContent = showing ? 'Show containers' : 'Hide containers';
        
        // Scroll to show containers if expanding
        if (showing && firstRow) {
          setTimeout(() => {
            firstRow.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          }, 100);
        }
      });
    });
  }

  /* ============================ connections view ========================== */

  async function loadConnections() {
    const containerId = state.connectionsContainerId;
    if (!containerId) return;

    const body = $('#connections-body');
    body.innerHTML =
      '<tr class="row-loading"><td colspan="7">Loading connections…</td></tr>';
    $('#connections-error').classList.add('hidden');
    $('#connections-summary').textContent = '';
    $('#connections-notes').textContent = '';

    try {
      state.connectionsData = await api("get", `/api/containers/${encodeURIComponent(containerId)}/connections?resolve=1`);
      renderConnections();
    } catch (error) {
      $('#connections-error').classList.remove('hidden');
      $('#connections-error').textContent = `Could not load connections: ${escapeHtml(
        errorMessage(error),
      )}`;
    }
  }

  function renderConnections() {
    const data = state.connectionsData;
    if (!data) return;

    const container = data.container || {};
    const netns = data.netns || {};
    const summary = data.summary || {};
    const connections = data.connections || [];
    const resolveIPs = state.connectionsResolveIPs;

    $('#connections-title').textContent = `Network connections — ${escapeHtml(container.name || container.id || '')}`;
    $('#connections-summary').textContent =
      `Total: ${summary.total} · TCP: ${summary.tcp} · UDP: ${summary.udp} · ` +
      `IPv4: ${summary.ipv4} · IPv6: ${summary.ipv6} · ` +
      `Listening: ${summary.listening} · Established: ${summary.established}`;

    const notes = [];
    if (netns.host_network) notes.push('Container uses host networking.');
    if (netns.shared_with && netns.shared_with.length) {
      const peers = netns.shared_with
        .map((c) => `${c.name} (${c.state})`)
        .join(', ');
      notes.push(`Shares network namespace with: ${peers}.`);
    }
    if (!connections.length) {
      notes.push('No TCP or UDP sockets found in this container\'s network namespace.');
    }
    $('#connections-notes').textContent = notes.join(' ');

    const query = $('#connection-search').value.trim().toLowerCase();
    const filtered = connections.filter((c) => {
      if (!query) return true;
      const haystack = `${c.protocol} ${c.family} ${c.state} ${c.local_address}:${c.local_port} ${c.remote_address}:${c.remote_port} ${c.uid} ${c.inode} ${
        c.local_resolved || ''
      } ${c.remote_resolved || ''}`.toLowerCase();
      return haystack.includes(query);
    });

    $('#connection-count').textContent = `${filtered.length} of ${connections.length} connection${
      connections.length === 1 ? '' : 's'
    }`;
    $('#connections-empty').classList.toggle('hidden', filtered.length > 0);

    $('#connections-body').innerHTML = filtered
      .map((c) => {
        const localHost = resolveIPs ? c.local_resolved || c.local_address : c.local_address;
        const remoteHost = resolveIPs ? c.remote_resolved || c.remote_address : c.remote_address;
        const local = `${escapeHtml(localHost)}:${escapeHtml(c.local_port)}`;
        const remote = `${escapeHtml(remoteHost)}:${escapeHtml(c.remote_port)}`;
        const canKill = c.state === 'ESTABLISHED' || c.state === 'LISTEN';
        return `
          <tr data-inode="${escapeHtml(c.inode)}" data-local-address="${escapeHtml(c.local_address)}" data-local-port="${escapeHtml(c.local_port)}" data-remote-address="${escapeHtml(c.remote_address)}" data-remote-port="${escapeHtml(c.remote_port)}">
            <td>${escapeHtml(c.protocol)}</td>
            <td>${escapeHtml(c.family)}</td>
            <td><span class="badge ${
              c.state === 'LISTEN' ? 'is-running' : c.state === 'ESTABLISHED' ? 'is-running' : 'is-other'
            }">${escapeHtml(c.state)}</span></td>
            <td title="${escapeHtml(c.local_address)}">${local}</td>
            <td title="${escapeHtml(c.remote_address)}">${remote}</td>
            <td>${escapeHtml(c.uid)}</td>
            <td class="mono">${escapeHtml(c.inode)}</td>
            <td class="col-actions">
              ${canKill ? '<button class="btn btn-sm btn-danger" data-action="kill-connection" type="button">Kill</button>' : '<span class="muted">—</span>'}
            </td>
          </tr>`;
      })
      .join('');

    if (resolveIPs && !state.connectionsResolvingIPs) {
      const unresolved = connections.some(
        (c) => [c.local_address, c.remote_address].some(
          (a) => a && a !== '0.0.0.0' && a !== '::' && !state.connectionsResolvedIPs[a],
        ),
      );
      if (unresolved) {
        // Kick off async IP resolution; it will re-render when complete.
        resolveConnectionIPs(filtered);
      }
    }
  }

  async function resolveConnectionIPs(connections) {
    state.connectionsResolvingIPs = true;
    const pending = [];
    for (const c of connections) {
      for (const addr of [c.local_address, c.remote_address]) {
        if (!addr || addr === '0.0.0.0' || addr === '::' || state.connectionsResolvedIPs[addr]) continue;
        pending.push(
          (async () => {
            try {
              const data = await api('get', `/api/resolve?ip=${encodeURIComponent(addr)}`);
              state.connectionsResolvedIPs[addr] = (data && data.hostname) || addr;
            } catch {
              state.connectionsResolvedIPs[addr] = addr;
            }
          })(),
        );
      }
    }
    await Promise.all(pending);
    state.connectionsResolvingIPs = false;
    // Re-render once resolutions are cached (guard prevents infinite loop)
    renderConnections();
  }

  async function openConnections(containerId) {
    state.connectionsContainerId = containerId;
    const container = state.containers.find((c) => c.id === containerId);
    if (container) state.connectionsContainer = container;
    showView('connections');
  }

  /* ============================ create payload ========================== */

  /* ============================ create payload ========================== */

  function collectSpec() {
    const errors = [];

    const name = $('#c-name').value.trim();
    if (name && !NAME_PATTERN.test(name)) {
      errors.push('Container name must match [a-zA-Z0-9][a-zA-Z0-9_.-]*');
    }

    const environment = {};
    $$('#env-rows .row').forEach((row) => {
      const key = row.querySelector('.env-key').value.trim();
      const value = row.querySelector('.env-value').value;
      if (!key && !value.trim()) return;
      if (!key) {
        errors.push('Environment variable name cannot be empty');
        return;
      }
      if (key in environment) errors.push(`Duplicate environment variable '${key}'`);
      environment[key] = value;
    });

    const ports = [];
    const portKeys = new Set();
    $$('#port-rows .row').forEach((row) => {
      const hostPort = row.querySelector('.port-host').value.trim();
      const containerPort = row.querySelector('.port-container').value.trim();
      const protocol = row.querySelector('.port-protocol').value;
      const hostIp = row.querySelector('.port-ip').value.trim();
      if (!hostPort && !containerPort) return;
      if (!containerPort) {
        errors.push('Container port is required for every port mapping');
        return;
      }
      const container = Number(containerPort);
      if (!Number.isInteger(container) || container < 1 || container > 65535) {
        errors.push(`Invalid container port '${containerPort}'`);
        return;
      }
      if (hostPort && (!Number.isInteger(Number(hostPort)) || Number(hostPort) < 1 || Number(hostPort) > 65535)) {
        errors.push(`Invalid host port '${hostPort}'`);
        return;
      }
      const key = `${container}/${protocol}`;
      if (portKeys.has(key)) errors.push(`Duplicate port mapping for ${key}`);
      portKeys.add(key);
      ports.push({
        container_port: container,
        host_port: hostPort ? Number(hostPort) : null,
        host_ip: hostIp || null,
        protocol,
      });
    });

    const mounts = [];
    const mountTargets = new Set();
    $$('#mount-rows .row').forEach((row) => {
      const type = row.querySelector('.mount-type').value;
      const bindInput = row.querySelector('.mount-source:not(.volume-select)');
      const volumeSelect = row.querySelector('.volume-select');
      const newVolumeInput = row.querySelector('.mount-source-new');

      let source;
      if (type.toLowerCase() === 'volume') {
        // Exactly one of the two controls is present: a text input for a
        // fresh volume name, or the dropdown of existing volumes.
        if (newVolumeInput) {
          source = newVolumeInput.value.trim();
          if (!source) {
            errors.push('New volume name is required');
            return;
          }
        } else if (volumeSelect && volumeSelect.value === '__new__') {
          errors.push('New volume name is required');
          return;
        } else {
          source = volumeSelect ? volumeSelect.value.trim() : '';
        }
      } else {
        source = bindInput.value.trim();
      }
      const target = row.querySelector('.mount-target').value.trim();
      const readOnly = row.querySelector('.mount-readonly').checked;
      if (!source && !target) return;
      if (!source || !target) {
        errors.push(`Both source and target are required for a ${type} mount`);
        return;
      }
      if (!target.startsWith('/')) {
        errors.push(`Mount target must be an absolute path: '${target}'`);
        return;
      }
      if (type.toLowerCase() === 'bind' && !/^(\/|[A-Za-z]:[\\/])/.test(source)) {
        errors.push(`Bind mount source must be an absolute path: '${source}'`);
        return;
      }
      if (mountTargets.has(target)) errors.push(`Duplicate mount target '${target}'`);
      mountTargets.add(target);
      mounts.push({ type: type.toLowerCase(), source, target, read_only: readOnly });
    });

    const networks = $$('#network-list input:checked').map((input) => input.value);

    return {
      spec: {
        image: state.image ? imageRef(state.image) : null,
        name: name || null,
        command: $('#c-command').value.trim() || null,
        entrypoint: $('#c-entrypoint').value.trim() || null,
        hostname: $('#c-hostname').value.trim() || null,
        restart_policy: $('#c-restart').value,
        environment,
        ports,
        mounts,
        networks,
      },
      errors,
    };
  }

  async function submitCreate(event) {
    event.preventDefault();
    if (state.busy || !state.image) return;

    const { spec, errors } = collectSpec();
    const status = $('#create-status');
    if (errors.length) {
      status.textContent = errors[0];
      toast('error', errors[0]);
      return;
    }

    setBusy(true);
    status.textContent = '';
    try {
      if (state.editContainerId) {
        // Edit mode: stop + remove the old container, then create a new one
        // with the edited parameters.
        await api('post', `/api/containers/${state.editContainerId}/stop`);
        await api('delete', `/api/containers/${state.editContainerId}?force=true`);
        state.editContainerId = null;
      }
      const created = await api('post', '/api/containers', spec);
      const label = created.name || created.short_id || 'container';
      toast('success', `Container “${label}” ${state.editContainerId ? 'recreated' : 'started'}`);
      (created.warnings || []).forEach((warning) => toast('error', warning));
      // Inform about networks that already have containers connected.
      (created.networks || []).forEach((network) => {
        const connected = (network.containers || [])
          .map((c) => c.name || c.id)
          .filter(Boolean);
        if (connected.length) {
          toast('info', `Network “${network.name}” already has containers: ${connected.join(', ')}`);
        }
      });
      showView('containers');
    } catch (error) {
      const message = errorMessage(error);
      status.textContent = message;
      showError(message);
    } finally {
      setBusy(false);
      state.editContainerId = null;
      $('#create-image').textContent = state.image ? imageLabel(state.image) : '';
    }
  }

  /* ------------------------------ containers --------------------------- */

  async function loadContainers() {
    const body = $('#containers-body');
    body.innerHTML = '<tr class="row-loading"><td colspan="6">Loading containers…</td></tr>';
    $('#containers-error').classList.add('hidden');

    try {
      state.containers = await api('get', '/api/containers');
    } catch (error) {
      $('#containers-empty').classList.add('hidden');
      const panel = $('#containers-error');
      const errorMsg = errorMessage(error);
      panel.textContent = errorMsg;
      panel.classList.remove('hidden');
      body.innerHTML = '';

      // Add helpful hint if backend is not available
      if (errorMsg.includes('Failed to fetch') || errorMsg.includes('NetworkError')) {
        panel.innerHTML += '<br><small class="muted">Backend not running. Start with: make run-backend</small>';
      }
      return;
    }
    renderContainers();
    markUpdated();
  }

  function renderContainers() {
    const query = $('#container-search').value.trim().toLowerCase();
    const containers = state.containers.filter((container) =>
      !query
        ? true
        : `${container.name} ${container.image} ${container.id} ${container.short_id}`
            .toLowerCase()
            .includes(query),
    );

    $('#container-count').textContent = `${containers.length} of ${state.containers.length} containers`;
    $('#containers-empty').classList.toggle('hidden', containers.length > 0);

    $('#containers-body').innerHTML = containers
      .map((container) => {
        const running = container.state === 'running';
        const badgeClass = running
          ? 'is-running'
          : container.state === 'exited' || container.state === 'created'
            ? 'is-exited'
            : 'is-other';
        const ports = container.ports.length
          ? container.ports.map((port) => `<div class="mono">${escapeHtml(port)}</div>`).join('')
          : '<span class="muted">—</span>';
        const networks = container.networks.length
          ? escapeHtml(container.networks.join(', '))
          : '<span class="muted">—</span>';
        return `
          <tr data-id="${escapeHtml(container.id)}">
            <td>
              <span class="cell-title">${escapeHtml(container.name)}</span>
              <span class="cell-sub mono">${escapeHtml(container.short_id)}</span>
            </td>
            <td>${escapeHtml(container.image)}</td>
            <td><span class="badge ${badgeClass}">${escapeHtml(container.state)}</span></td>
            <td>${ports}</td>
            <td>${networks}</td>
            <td class="col-actions">
              <div style="display:flex;gap:6px;justify-content:flex-end">
                <button class="btn btn-sm" data-action="connections" type="button">Connections</button>
                <button class="btn btn-sm" data-action="networks" type="button">Networks</button>
                <button class="btn btn-sm" data-action="logs" type="button">Logs</button>
                <button class="btn btn-sm btn-ghost" data-action="edit" type="button">Edit</button>
                ${
                  running
                    ? '<button class="btn btn-sm" data-action="restart" type="button">Restart</button>\n                <button class="btn btn-sm" data-action="stop" type="button">Stop</button>'
                    : '<button class="btn btn-sm" data-action="start" type="button">Start</button>'
                }
                <button class="btn btn-sm btn-danger" data-action="remove" type="button">Remove</button>
              </div>
            </td>
          </tr>`;
      })
      .join('');
  }

  function confirmAction(button, action) {
    if (button.dataset.armed === '1') {
      delete button.dataset.armed;
      action();
      return;
    }
    button.dataset.armed = '1';
    button.textContent = 'Sure?';
    setTimeout(() => {
      if (button.dataset.armed === '1') {
        delete button.dataset.armed;
        button.textContent = 'Remove';
      }
    }, CONFIRM_TIMEOUT);
  }

  async function containerAction(action, id, button) {
    try {
      if (action === 'remove') {
        await api('delete', `/api/containers/${id}?force=true`);
        toast('success', 'Container removed');
      } else if (action === 'stop') {
        await api('post', `/api/containers/${id}/stop`);
        toast('success', 'Container stopping…');
      } else if (action === 'start') {
        await api('post', `/api/containers/${id}/start`);
        toast('success', 'Container started');
      } else if (action === 'restart') {
        await api('post', `/api/containers/${id}/restart`);
        toast('success', 'Container restarting');
      }
      await loadContainers();
    } catch (error) {
      if (button) delete button.dataset.armed;
      showError(error);
    }
  }

  /* ------------------------------- layers ------------------------------- */

  function renderLayers(image) {
    const list = $('#layers-list');
    const layers = image.layers || [];
    if (!layers.length) {
      list.innerHTML = '<div class="empty"><p class="empty-title">No layer history available</p></div>';
      return;
    }

    // Show newest layer first (the API list is oldest first).
    const entries = layers.slice().reverse();
    list.innerHTML = entries
      .map((layer, index) => {
        const position = entries.length - index; // 1 = oldest layer
        const isFsLayer = layer.size > 0;
        const command = layer.created_by
          ? escapeHtml(layer.created_by)
          : `<span class="muted">${layer.comment ? escapeHtml(layer.comment) : '—'}</span>`;
        const tags = (layer.tags || [])
          .map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`)
          .join('');
        return `
          <div class="layer-entry">
            <span class="layer-index mono" title="Layer ${position} of ${entries.length}">#${position}</span>
            <div class="layer-main">
              <div class="layer-command mono">${command}${tags ? ` ${tags}` : ''}</div>
              <div class="muted">${isFsLayer ? formatBytes(layer.size) : 'metadata (no filesystem change)'} · ${timeAgo(layer.created)}</div>
            </div>
          </div>`;
      })
      .join('');
  }

  async function openLayers(image) {
    const ref = imageRef(image);
    $('#layers-title').textContent = `Layers — ${imageLabel(image)}`;
    $('#layers-info').textContent = '';
    $('#layers-list').innerHTML = '<div class="muted">Loading layers…</div>';
    $('#layers-status').textContent = '';
    $('#layers-modal').classList.remove('hidden');

    try {
      const data = await api('get', `/api/images/${encodeURIComponent(ref)}/layers`);
      $('#layers-info').textContent =
        `${data.layer_count} filesystem layer(s) · total size ${formatBytes(data.size)}`;
      renderLayers(data);
      $('#layers-status').textContent = `${(data.layers || []).length} history entries`;
    } catch (error) {
      $('#layers-list').innerHTML = '';
      $('#layers-status').textContent = 'Error';
      $('#layers-modal').classList.add('hidden');
      showError(error);
    }
  }

  function closeLayers() {
    $('#layers-modal').classList.add('hidden');
  }

  /* ------------------------------- docs --------------------------------- */

  let docsRequestId = 0;

  async function openDocs() {
    if (!state.image) return;
    const requestId = ++docsRequestId;
    const ref = imageRef(state.image);

    $('#docs-title').textContent = `Documentation — ${imageLabel(state.image)}`;
    $('#docs-body').innerHTML = '<div class="muted">Loading documentation from Docker Hub…</div>';
    $('#docs-meta').textContent = '';
    $('#docs-hub-link').classList.add('hidden');
    $('#docs-hub-link').href = '#';
    $('#docs-modal').classList.remove('hidden');

    try {
      const data = await api('get', `/api/images/${encodeURIComponent(ref)}/documentation`);
      if (requestId !== docsRequestId) return; // modal was closed meanwhile
      $('#docs-body').innerHTML = renderMarkdown(data.markdown);
      $('#docs-hub-link').href = data.url;
      $('#docs-hub-link').classList.remove('hidden');
      const meta = [];
      if (data.official) meta.push('Official image');
      if (data.pull_count) meta.push(`${Number(data.pull_count).toLocaleString()} pulls`);
      if (data.last_updated) meta.push(`updated ${new Date(data.last_updated).toLocaleDateString()}`);
      $('#docs-meta').textContent = meta.join(' · ');
    } catch (error) {
      if (requestId !== docsRequestId) return;
      $('#docs-body').innerHTML =
        `<div class="empty"><p class="empty-title">${escapeHtml(errorMessage(error))}</p>` +
        '<p class="muted">Documentation is fetched from Docker Hub and needs a named image.</p></div>';
      $('#docs-meta').textContent = 'Error';
    }
  }

  function closeDocs() {
    docsRequestId += 1;
    $('#docs-modal').classList.add('hidden');
  }

  /* ------------------------------- logs -------------------------------- */

  let logsSource = null;
  let logsPollTimer = null;
  let logsContainerId = null;
  let logsFollow = false;
  let logsFirstLoad = true;

  const logsUrl = (containerId, tail = null) => {
    let url = `${httpBase}/api/containers/${encodeURIComponent(containerId)}/logs?follow=true`;
    if (tail !== null) {
      url += `&tail=${tail}`;
    }
    return url;
  };

  async function loadLogs() {
    if (!logsContainerId) return;
    try {
      const tail = logsFirstLoad ? 100 : null;
      const url = tail !== null
        ? `/api/containers/${encodeURIComponent(logsContainerId)}/logs?tail=${tail}`
        : `/api/containers/${encodeURIComponent(logsContainerId)}/logs`;
      const data = await api('get', url);
      const lines = (data && data.logs) || [];
      const output = $('#log-output');
      output.textContent = lines.join('\n');
      if (logsFollow) output.scrollTop = output.scrollHeight;
      $('#log-status').textContent = 'Streaming…';
      logsFirstLoad = false;
    } catch (error) {
      $('#log-status').textContent = 'Error';
      showError(error);
    }
  }

  function openLogs(containerId) {
    logsContainerId = containerId;
    logsFollow = true;
    logsFirstLoad = true;
    setLogsFollowUI(true);
    $('#log-title').textContent = `Logs — ${containerId}`;
    $('#log-status').textContent = 'Connecting…';
    $('#log-output').textContent = '';
    $('#log-modal').classList.remove('hidden');

    if (logsSource) {
      logsSource.close();
      logsSource = null;
    }
    if (logsPollTimer) {
      clearInterval(logsPollTimer);
      logsPollTimer = null;
    }

    if (vmService) {
      loadLogs(); // First load with tail=100
      if (logsFollow) {
        logsPollTimer = setInterval(() => loadLogs(), LOGS_POLL_INTERVAL);
      }
    } else {
      logsSource = new EventSource(logsUrl(containerId, 100));
      logsSource.addEventListener('open', () => {
        $('#log-status').textContent = 'Streaming…';
      });
      logsSource.addEventListener('log', (event) => {
        const output = $('#log-output');
        const line = event.data || '';
        if (output.textContent && !output.textContent.endsWith('\n')) output.textContent += '\n';
        output.textContent += line;
        if (logsFollow) output.scrollTop = output.scrollHeight;
        $('#log-status').textContent = 'Streaming…';
      });
      logsSource.addEventListener('error', () => {
        $('#log-status').textContent = logsSource.readyState === 2 ? 'Reconnecting…' : 'Streaming…';
      });
    }
  }

  function closeLogs() {
    if (logsSource) {
      logsSource.close();
      logsSource = null;
    }
    if (logsPollTimer) {
      clearInterval(logsPollTimer);
      logsPollTimer = null;
    }
    $('#log-modal').classList.add('hidden');
    $('#log-status').textContent = '';
  }

  function setLogsFollow(enabled) {
    logsFollow = enabled;
    setLogsFollowUI(enabled);
    if (logsSource) {
      logsSource.close();
      logsSource = null;
    }
    if (logsPollTimer) {
      clearInterval(logsPollTimer);
      logsPollTimer = null;
    }
    if (enabled && logsContainerId) {
      if (vmService) {
        logsPollTimer = setInterval(() => loadLogs(), LOGS_POLL_INTERVAL);
        loadLogs();
      } else {
        openLogs(logsContainerId);
      }
    }
  }

  function setLogsFollowUI(enabled) {
    const button = $('#log-follow-btn');
    if (button) button.textContent = `Follow: ${enabled ? 'on' : 'off'}`;
  }

  /* -------------------------------- wiring ----------------------------- */

  function bindEvents() {
    $$('.nav-btn').forEach((button) =>
      button.addEventListener('click', () => showView(button.dataset.view)),
    );

    $('#refresh-btn').addEventListener('click', () => {
      if (state.view === 'containers') loadContainers();
      else loadImages();
    });

    $('#live-btn').addEventListener('click', () => toggleLive());

    $('#image-search').addEventListener('input', renderImages);
    $('#container-search').addEventListener('input', renderContainers);

    $('#images-body').addEventListener('click', (event) => {
      const button = event.target.closest('[data-action]');
      if (!button) return;
      const row = button.closest('tr');
      const image = state.images.find((item) => imageLabel(item) === row.dataset.image);
      if (button.dataset.action === 'run') {
        if (image) openCreate(image);
        return;
      }
      if (button.dataset.action === 'layers') {
        if (image) openLayers(image);
        return;
      }
      if (button.dataset.action === 'remove-image') {
        if (image) deleteImage(image, button);
        return;
      }
    });

    $('#containers-body').addEventListener('click', (event) => {
      const button = event.target.closest('[data-action]');
      if (!button) return;
      const id = button.closest('tr').dataset.id;
      const { action } = button.dataset;
      if (action === 'remove') {
        confirmAction(button, () => containerAction(action, id, button));
      } else if (action === 'logs') {
        openLogs(id);
      } else if (action === 'restart') {
        containerAction(action, id, button);
      } else if (action === 'edit') {
        const container = state.containers.find((c) => c.id === id);
        if (container) openEdit(container);
      } else if (action === 'connections') {
        openConnections(id);
      } else if (action === 'networks') {
        showView('networks');
      } else {
        containerAction(action, id, button);
      }
    });

    $('#back-btn').addEventListener('click', () => showView('images'));
    $('#networks-back-btn').addEventListener('click', () => showView('containers'));
    $('#networks-refresh-btn').addEventListener('click', () => loadNetworksList());
    $('#network-search').addEventListener('input', renderNetworksList);
    $('#connections-back-btn').addEventListener('click', () => showView('containers'));
    $('#connections-refresh-btn').addEventListener('click', () => loadConnections());
    $('#connection-search').addEventListener('input', renderConnections);
    $('#connections-body').addEventListener('click', async (event) => {
      const button = event.target.closest('[data-action]');
      if (!button) return;
      const row = button.closest('tr');
      const inode = row.dataset.inode;
      const localAddress = row.dataset.localAddress;
      const localPort = row.dataset.localPort;
      const remoteAddress = row.dataset.remoteAddress;
      const remotePort = row.dataset.remotePort;
      const { action } = button.dataset;
      if (action === 'kill-connection') {
        try {
          const params = new URLSearchParams({ inode });
          if (localAddress) params.set('local_address', localAddress);
          if (localPort) params.set('local_port', localPort);
          if (remoteAddress) params.set('remote_address', remoteAddress);
          if (remotePort) params.set('remote_port', remotePort);
          const result = await api('post', `/api/containers/${encodeURIComponent(state.connectionsContainerId)}/connections/kill?${params}`);
          console.log('Kill result:', result);

          if (result.killed) {
            toast('success', 'Connection killed');
            loadConnections();
          } else {
            // Show debug modal if not killed
            if (result.debug && result.debug.length > 0) {
              $('#debug-output').textContent = result.debug.join('\n\n');
              $('#debug-modal').classList.remove('hidden');
            }
            toast('error', 'Failed to kill connection - see debug output');
          }
        } catch (error) {
          showError(error);
        }
      }
    });

    $('#debug-close').addEventListener('click', () => {
      $('#debug-modal').classList.add('hidden');
    });
    $('#debug-close-btn').addEventListener('click', () => {
      $('#debug-modal').classList.add('hidden');
    });
    $('#resolve-ips').addEventListener('change', (event) => {
      state.connectionsResolveIPs = event.target.checked;
      if (state.connectionsResolveIPs && state.connectionsData) {
        renderConnections();
      }
    });
    $('#create-form').addEventListener('submit', submitCreate);
    $('#add-env').addEventListener('click', () => addEnvRow());
    $('#add-port').addEventListener('click', () => addPortRow());
    $('#add-mount').addEventListener('click', () => addMountRow());
    $('#log-close').addEventListener('click', closeLogs);
    $('#log-follow-btn').addEventListener('click', () => setLogsFollow(!logsFollow));
    $('#layers-close').addEventListener('click', closeLayers);
    $('#layers-modal').addEventListener('click', (event) => {
      if (event.target === event.currentTarget) closeLayers();
    });
    $('#docs-btn').addEventListener('click', openDocs);
    $('#docs-close').addEventListener('click', closeDocs);
    $('#docs-close-btn').addEventListener('click', closeDocs);
    $('#docs-modal').addEventListener('click', (event) => {
      if (event.target === event.currentTarget) closeDocs();
    });

    // Escape closes whichever modal is currently open.
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      const open = document.querySelector('.modal-backdrop:not(.hidden)');
      if (!open) return;
      const closer = open.querySelector('button[id$="-close"], #docs-close-btn');
      if (closer) closer.click();
    });
  }

  function markUpdated() {
    const element = $('#last-updated');
    if (element) element.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }

  function setLiveUI(enabled) {
    const button = $('#live-btn');
    if (!button) return;
    button.textContent = `Live: ${enabled ? 'on' : 'off'}`;
    button.setAttribute('aria-pressed', enabled ? 'true' : 'false');
    if (enabled) button.classList.add('is-active'); else button.classList.remove('is-active');
  }

  function toggleLive() {
    state.live = !state.live;
    setLiveUI(state.live);
    if (state.liveTimer) {
      clearInterval(state.liveTimer);
      state.liveTimer = null;
    }
    if (state.live) {
      state.liveTimer = setInterval(() => {
        if (state.view === 'containers') loadContainers();
        else if (state.view === 'images') loadImages();
      }, LIVE_POLL_INTERVAL);
    }
  }

  async function loadEngineInfo() {
    const element = $('#engine-info');
    const warning = $('#backend-warning');
    try {
      const health = await api('get', '/api/health');
      const engine = health.engine || {};
      element.textContent = `Docker ${engine.version || ''}`.trim();
      element.title = `Engine ${engine.version} · API ${engine.api_version} · ${engine.os}/${engine.arch}`;
      if (warning) warning.classList.add('hidden');
    } catch (error) {
      const errorMsg = errorMessage(error);
      element.textContent = 'backend unavailable';
      element.title = errorMsg;

      // Show connection error hint if backend is not reachable
      if (errorMsg.includes('Failed to fetch') || errorMsg.includes('NetworkError')) {
        element.textContent = 'backend not running';
        element.title = 'Backend not running. Start with: make run-backend';
        if (warning) warning.classList.remove('hidden');
      }
    }
  }

  function init() {
    if (!vmService && !window.fetch) {
      document.body.innerHTML = '<p style="padding:24px">Docker Desktop API not available.</p>';
      return;
    }
    bindEvents();
    resetCreateForm();
    setLiveUI(false);
    showView('images'); // triggers the initial images + engine load
    loadEngineInfo();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
