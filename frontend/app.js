const state = {
  allMatches: [],
  displayedMatches: [],
  currentScan: null,
  currentScanId: null,
  currentRepo: null,
  eventSource: null,
  settings: {},
  settingsSchema: {},
  settingsLoaded: false,
  rateLimit: null,
};

const RATE_LIMIT_POLL_INTERVAL_MS = 15000;

const SETTINGS_FIELD_ORDER = [
  'GITHUB_TOKEN',
  'TARGET_MATCHES',
  'MIN_STARS',
  'MIN_FILES_CHANGED',
  'MAX_REPO_PAGES',
  'MAX_PULL_PAGES_PER_REPO',
  'REPOS_PER_PAGE',
  'PULLS_PER_PAGE',
  'REQUEST_TIMEOUT',
  'RUN_UNTIL_STOP',
  'FULL_SWEEP_PAUSE_SECONDS',
];

const elements = {
  totalMatches: document.getElementById('totalMatches'),
  displayedMatches: document.getElementById('displayedMatches'),
  scanStatus: document.getElementById('scanStatus'),
  rateLimitCard: document.getElementById('rateLimitCard'),
  rateLimitRemaining: document.getElementById('rateLimitRemaining'),
  rateLimitStatus: document.getElementById('rateLimitStatus'),
  rateLimitReset: document.getElementById('rateLimitReset'),
  activeRepo: document.getElementById('activeRepo'),
  newMatchesCount: document.getElementById('newMatchesCount'),
  scanId: document.getElementById('scanId'),
  repoInput: document.getElementById('repoInput'),
  scanButton: document.getElementById('scanButton'),
  scanHint: document.getElementById('scanHint'),
  openSettingsButton: document.getElementById('openSettingsButton'),
  startLiveScanButton: document.getElementById('startLiveScanButton'),
  stopLiveScanButton: document.getElementById('stopLiveScanButton'),
  refreshMatchesButton: document.getElementById('refreshMatchesButton'),
  clearLogButton: document.getElementById('clearLogButton'),
  filterInput: document.getElementById('filterInput'),
  sortSelect: document.getElementById('sortSelect'),
  matchesGrid: document.getElementById('matchesGrid'),
  emptyState: document.getElementById('emptyState'),
  logOutput: document.getElementById('logOutput'),
  scanForm: document.getElementById('scanForm'),
  matchCardTemplate: document.getElementById('matchCardTemplate'),
  settingsLayer: document.getElementById('settingsLayer'),
  settingsBackdrop: document.getElementById('settingsBackdrop'),
  settingsDrawer: document.getElementById('settingsDrawer'),
  settingsForm: document.getElementById('settingsForm'),
  settingsGrid: document.getElementById('settingsGrid'),
  settingsStatus: document.getElementById('settingsStatus'),
  reloadSettingsButton: document.getElementById('reloadSettingsButton'),
  closeSettingsButton: document.getElementById('closeSettingsButton'),
  saveSettingsButton: document.getElementById('saveSettingsButton'),
};

function isSettingsModalOpen() {
  return !elements.settingsLayer.classList.contains('hidden');
}

function openSettingsModal() {
  elements.settingsLayer.classList.remove('hidden');
  elements.settingsLayer.setAttribute('aria-hidden', 'false');
  document.body.classList.add('modal-open');
}

function closeSettingsModal() {
  elements.settingsLayer.classList.add('hidden');
  elements.settingsLayer.setAttribute('aria-hidden', 'true');
  document.body.classList.remove('modal-open');
}

async function ensureSettingsLoaded(forceReload = false) {
  if (state.settingsLoaded && !forceReload) {
    return;
  }

  setSettingsBusy(true);
  setSettingsStatus(forceReload ? 'Reloading settings...' : 'Loading settings...', 'info');
  try {
    await fetchSettings();
    state.settingsLoaded = true;
  } finally {
    setSettingsBusy(false);
  }
}

function formatSettingLabel(name) {
  return name
    .toLowerCase()
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function formatDuration(seconds) {
  if (seconds == null) {
    return '--';
  }

  const totalSeconds = Math.max(Number.parseInt(seconds, 10) || 0, 0);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainingSeconds = totalSeconds % 60;

  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }

  if (minutes > 0) {
    return `${minutes}m ${remainingSeconds}s`;
  }

  return `${remainingSeconds}s`;
}

function renderRateLimit() {
  const rateLimit = state.rateLimit;
  if (!rateLimit) {
    elements.rateLimitCard.dataset.status = 'unknown';
    elements.rateLimitRemaining.textContent = '-- / --';
    elements.rateLimitStatus.textContent = 'Checking quota...';
    elements.rateLimitReset.textContent = 'Reset: --';
    return;
  }

  const remaining = rateLimit.remaining ?? '--';
  const limit = rateLimit.limit ?? '--';
  const resetText = formatDuration(rateLimit.reset_in_seconds);
  const statusText = {
    healthy: 'Healthy',
    warning: 'Getting low',
    low: 'Low quota',
    exhausted: 'Exhausted until reset',
    refreshing: 'Refreshing',
    unknown: 'Unknown',
  }[rateLimit.status] || 'Unknown';

  elements.rateLimitCard.dataset.status = rateLimit.status || 'unknown';
  elements.rateLimitRemaining.textContent = `${remaining} / ${limit}`;
  if (rateLimit.usage_percent != null) {
    elements.rateLimitStatus.textContent = `${statusText} • ${rateLimit.usage_percent}% used`;
  } else {
    elements.rateLimitStatus.textContent = statusText;
  }
  elements.rateLimitReset.textContent = `Reset: ${resetText}`;
}

async function fetchRateLimit(refresh = false) {
  const url = new URL('/api/github-rate-limit', window.location.origin);
  if (refresh) {
    url.searchParams.set('refresh', 'true');
  }

  const response = await fetch(url, { cache: 'no-store' });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (payload?.rate_limit) {
    state.rateLimit = payload.rate_limit;
    renderRateLimit();
  }

  if (!response.ok) {
    throw new Error(payload?.error || 'Failed to load GitHub rate limit');
  }

  return state.rateLimit;
}

function setSettingsStatus(message, kind = 'info') {
  elements.settingsStatus.textContent = message;
  elements.settingsStatus.dataset.kind = kind;
}

function setSettingsBusy(isBusy) {
  elements.saveSettingsButton.disabled = isBusy;
  elements.reloadSettingsButton.disabled = isBusy;
}

function createSettingField(name, schema, value) {
  const field = document.createElement('div');
  field.className = 'settings-field';

  const label = document.createElement('label');
  label.className = 'settings-label';
  label.htmlFor = `setting-${name}`;
  label.textContent = formatSettingLabel(name);

  const key = document.createElement('span');
  key.className = 'settings-key';
  key.textContent = name;

  field.appendChild(label);
  field.appendChild(key);

  let input;
  if (schema.type === 'bool') {
    const toggleWrap = document.createElement('label');
    toggleWrap.className = 'settings-toggle';

    input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = Boolean(value);

    const toggleText = document.createElement('span');
    toggleText.textContent = value ? 'Enabled' : 'Disabled';

    input.addEventListener('change', () => {
      toggleText.textContent = input.checked ? 'Enabled' : 'Disabled';
    });

    toggleWrap.appendChild(input);
    toggleWrap.appendChild(toggleText);
    field.appendChild(toggleWrap);
  } else {
    input = document.createElement('input');
    input.type = schema.sensitive ? 'password' : schema.type === 'int' ? 'number' : 'text';
    input.value = value ?? '';
    input.placeholder = schema.default === '' ? '' : String(schema.default ?? '');
    input.autocomplete = schema.sensitive ? 'off' : 'on';

    if (schema.type === 'int') {
      input.inputMode = 'numeric';
      input.step = '1';
      input.required = true;
    }

    field.appendChild(input);
  }

  input.id = `setting-${name}`;
  input.name = name;
  input.dataset.type = schema.type;

  return field;
}

function renderSettingsForm() {
  elements.settingsGrid.innerHTML = '';

  SETTINGS_FIELD_ORDER.forEach((name) => {
    const schema = state.settingsSchema[name];
    if (!schema) {
      return;
    }

    const value = Object.prototype.hasOwnProperty.call(state.settings, name)
      ? state.settings[name]
      : schema.default;
    elements.settingsGrid.appendChild(createSettingField(name, schema, value));
  });
}

function collectSettingsFormData() {
  const settings = {};

  SETTINGS_FIELD_ORDER.forEach((name) => {
    const schema = state.settingsSchema[name];
    const input = elements.settingsForm.elements.namedItem(name);
    if (!schema || !input) {
      return;
    }

    if (schema.type === 'bool') {
      settings[name] = Boolean(input.checked);
      return;
    }

    const rawValue = input.value.trim();
    if (schema.type === 'int') {
      if (!rawValue) {
        throw new Error(`${formatSettingLabel(name)} is required.`);
      }

      const numericValue = Number.parseInt(rawValue, 10);
      if (Number.isNaN(numericValue)) {
        throw new Error(`${formatSettingLabel(name)} must be a whole number.`);
      }

      settings[name] = numericValue;
      return;
    }

    settings[name] = rawValue;
  });

  return settings;
}

function formatDate(value) {
  if (!value) {
    return 'Unknown';
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  }).format(date);
}

function relativeMergedText(match) {
  const issueNumber = match.issue_number ?? '-';
  const prNumber = match.pr_number ?? '-';
  return `Issue #${issueNumber} paired with PR #${prNumber}`;
}

function appendLogLine(message, kind = 'log') {
  const line = document.createElement('div');
  line.className = `log-line ${kind}`;
  line.textContent = message;
  elements.logOutput.appendChild(line);
  elements.logOutput.scrollTop = elements.logOutput.scrollHeight;
}

function clearLog() {
  elements.logOutput.innerHTML = '';
}

async function extractApiError(response, fallbackMessage) {
  let payload = null;

  try {
    payload = await response.json();
  } catch {
    return `${fallbackMessage} (HTTP ${response.status})`;
  }

  const detail = payload?.detail;
  if (typeof detail === 'string' && detail.trim()) {
    return detail;
  }

  if (detail && typeof detail === 'object' && typeof detail.message === 'string' && detail.message.trim()) {
    return detail.message;
  }

  if (typeof payload?.message === 'string' && payload.message.trim()) {
    return payload.message;
  }

  return `${fallbackMessage} (HTTP ${response.status})`;
}

function isScanActive(scan) {
  return Boolean(scan && ['queued', 'running', 'stopping'].includes(scan.status));
}

function updateScanControls(scan) {
  const active = isScanActive(scan);
  const isContinuous = active && scan.mode === 'continuous';
  const canStop = Boolean(scan && scan.can_stop);
  const rateLimitExhausted = !active && state.rateLimit?.status === 'exhausted';

  elements.scanButton.disabled = active || rateLimitExhausted;
  elements.repoInput.disabled = active;
  elements.startLiveScanButton.disabled = active || rateLimitExhausted;
  elements.stopLiveScanButton.disabled = !(isContinuous && canStop);

  if (rateLimitExhausted) {
    elements.scanHint.textContent = 'GitHub rate limit is exhausted. New scans are paused until the reset timer finishes.';
  } else {
    elements.scanHint.textContent = 'The scan streams PR pages live below and saves qualified matches to disk immediately.';
  }
}

function setScanMeta(scan) {
  state.currentScan = scan;
  if (!scan) {
    elements.scanStatus.textContent = 'Idle';
    elements.activeRepo.textContent = 'None';
    elements.newMatchesCount.textContent = '0';
    elements.scanId.textContent = '-';
    updateScanControls(null);
    return;
  }

  elements.scanStatus.textContent = scan.status || 'Idle';
  elements.activeRepo.textContent = scan.repo || 'None';
  elements.newMatchesCount.textContent = String(scan.new_match_count || 0);
  elements.scanId.textContent = scan.scan_id || '-';
  updateScanControls(scan);
}

function updateStats() {
  elements.totalMatches.textContent = String(state.allMatches.length);
  elements.displayedMatches.textContent = String(state.displayedMatches.length);
}

function copyText(text) {
  if (!text) {
    return;
  }

  navigator.clipboard.writeText(text).then(() => {
    appendLogLine(`Copied checkout SHA ${text}`, 'status');
  }).catch(() => {
    appendLogLine('Clipboard copy failed.', 'error');
  });
}

function toggleMatchDetails(card, toggleButton, expandable) {
  const isExpanded = card.classList.toggle('expanded');
  expandable.classList.toggle('hidden', !isExpanded);
  toggleButton.setAttribute('aria-expanded', String(isExpanded));
  toggleButton.textContent = isExpanded ? 'Hide details' : 'Show details';
}

function getMatchTimestamp(match) {
  const candidate = match.pr_merged_at || match.issue_closed_at;
  if (!candidate) {
    return 0;
  }

  const parsed = Date.parse(candidate);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function sortMatches(matches) {
  const sortMode = elements.sortSelect?.value || 'recent';
  const sorted = [...matches];

  sorted.sort((left, right) => {
    if (sortMode === 'repo-asc') {
      return (left.repo || '').localeCompare(right.repo || '');
    }

    if (sortMode === 'repo-desc') {
      return (right.repo || '').localeCompare(left.repo || '');
    }

    if (sortMode === 'files-asc') {
      return (left.files_changed || 0) - (right.files_changed || 0);
    }

    if (sortMode === 'files-desc') {
      return (right.files_changed || 0) - (left.files_changed || 0);
    }

    return getMatchTimestamp(right) - getMatchTimestamp(left);
  });

  return sorted;
}

function renderMatches() {
  const query = elements.filterInput.value.trim().toLowerCase();
  const filteredMatches = state.allMatches.filter((match) => {
    if (!query) {
      return true;
    }

    const haystack = [
      match.repo,
      match.issue_title,
      match.issue_url,
      match.pr_url,
      match.checkout_sha,
      match.base_ref,
    ]
      .filter(Boolean)
      .join(' ')
      .toLowerCase();

    return haystack.includes(query);
  });

  state.displayedMatches = sortMatches(filteredMatches);

  updateStats();
  elements.matchesGrid.innerHTML = '';

  if (!state.displayedMatches.length) {
    elements.emptyState.classList.remove('hidden');
    return;
  }

  elements.emptyState.classList.add('hidden');

  state.displayedMatches.forEach((match) => {
    const fragment = elements.matchCardTemplate.content.cloneNode(true);
    fragment.querySelector('.repo-pill').textContent = match.repo || 'Unknown repo';
    fragment.querySelector('.files-pill').textContent = `${match.files_changed || 0} files changed`;
    fragment.querySelector('.issue-title').textContent = match.issue_title || 'Untitled issue';
    fragment.querySelector('.issue-meta').textContent = relativeMergedText(match);
    fragment.querySelector('.checkout-sha').textContent = match.checkout_sha || 'Unavailable';
    fragment.querySelector('.base-ref').textContent = match.base_ref || 'Unknown';
    fragment.querySelector('.merged-at').textContent = formatDate(match.pr_merged_at || match.issue_closed_at);
    fragment.querySelector('.issue-comments').textContent = String(match.issue_comments || 0);

    const card = fragment.querySelector('.match-card');
    const toggleButton = fragment.querySelector('.details-toggle');
    const expandable = fragment.querySelector('.match-expandable');

    const issueLink = fragment.querySelector('.issue-link');
    issueLink.href = match.issue_url || '#';
    const prLink = fragment.querySelector('.pr-link');
    prLink.href = match.pr_url || '#';

    fragment.querySelector('.copy-button').addEventListener('click', () => copyText(match.checkout_sha));
    toggleButton.addEventListener('click', () => toggleMatchDetails(card, toggleButton, expandable));
    elements.matchesGrid.appendChild(fragment);
  });
}

async function fetchMatches(repo = null) {
  const url = new URL('/api/matches', window.location.origin);
  if (repo) {
    url.searchParams.set('repo', repo);
  }

  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error('Failed to load matches');
  }

  const payload = await response.json();
  state.allMatches = payload.matches || [];
  renderMatches();
}

async function fetchScanStatus() {
  const response = await fetch('/api/scan-status', { cache: 'no-store' });
  if (!response.ok) {
    throw new Error('Failed to load scan status');
  }

  const payload = await response.json();
  return payload.scan;
}

async function fetchSettings() {
  const response = await fetch('/api/settings', { cache: 'no-store' });
  if (!response.ok) {
    throw new Error('Failed to load settings');
  }

  const payload = await response.json();
  state.settings = payload.settings || {};
  state.settingsSchema = payload.schema || {};
  renderSettingsForm();
  setSettingsStatus('Settings loaded from the backend.', 'success');
}

async function saveSettings() {
  const settings = collectSettingsFormData();
  setSettingsBusy(true);
  setSettingsStatus('Saving settings...', 'info');

  try {
    const response = await fetch('/api/settings', {
      method: 'PATCH',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ settings }),
    });

    if (!response.ok) {
      throw new Error(await extractApiError(response, 'Settings could not be saved.'));
    }

    const payload = await response.json();
    state.settings = payload.settings || {};
    state.settingsSchema = payload.schema || state.settingsSchema;
    renderSettingsForm();
    state.settingsLoaded = true;
    setSettingsStatus('Settings saved to the local .env file.', 'success');
    appendLogLine('Scanner settings updated.', 'status');
  } finally {
    setSettingsBusy(false);
  }
}

function closeEventSource() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
}

function connectToScanStream(scanId) {
  if (!scanId) {
    return;
  }

  closeEventSource();
  state.currentScanId = scanId;
  const url = new URL('/api/scan-events', window.location.origin);
  url.searchParams.set('scan_id', scanId);
  const eventSource = new EventSource(url);
  state.eventSource = eventSource;

  eventSource.addEventListener('log', (event) => {
    const payload = JSON.parse(event.data);
    appendLogLine(payload.message || '', 'log');
  });

  eventSource.addEventListener('status', async (event) => {
    const payload = JSON.parse(event.data);
    setScanMeta({ ...(state.currentScan || {}), ...payload, scan_id: state.currentScanId });
    if (payload.repo) {
      state.currentRepo = payload.repo;
    }
  });

  eventSource.addEventListener('summary', async (event) => {
    const payload = JSON.parse(event.data);
    setScanMeta(payload);
    if (payload.mode === 'repo' && payload.repo) {
      elements.filterInput.value = payload.repo;
      await fetchMatches();
    } else {
      await fetchMatches();
    }

    appendLogLine(`Scan ${payload.status} for ${payload.repo}.`, payload.status === 'error' ? 'error' : 'status');
    closeEventSource();
  });

  eventSource.onerror = () => {
    appendLogLine('Live scan stream disconnected.', 'error');
    closeEventSource();
    updateScanControls(state.currentScan);
  };
}

async function startRepoScan(repo) {
  await fetchRateLimit(true);
  if (state.rateLimit?.status === 'exhausted') {
    throw new Error('GitHub rate limit is exhausted. Wait for the reset timer before starting a new scan.');
  }

  const response = await fetch('/api/scan-repo', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ repo }),
  });

  if (!response.ok) {
    throw new Error(await extractApiError(response, 'Scan could not be started.'));
  }

  const payload = await response.json();

  const scan = payload.scan;
  setScanMeta(scan);
  clearLog();
  appendLogLine(`Preparing scan for ${scan.repo}`, 'status');
  connectToScanStream(scan.scan_id);
}

async function startLiveScan() {
  await fetchRateLimit(true);
  if (state.rateLimit?.status === 'exhausted') {
    throw new Error('GitHub rate limit is exhausted. Wait for the reset timer before starting live scan.');
  }

  const response = await fetch('/api/scan-live/start', {
    method: 'POST',
  });

  if (!response.ok) {
    throw new Error(await extractApiError(response, 'Live scan could not be started.'));
  }

  const payload = await response.json();

  const scan = payload.scan;
  setScanMeta(scan);
  clearLog();
  appendLogLine('Preparing nonstop discovery scan.', 'status');
  connectToScanStream(scan.scan_id);
}

async function stopLiveScan() {
  const response = await fetch('/api/scan-live/stop', {
    method: 'POST',
  });

  if (!response.ok) {
    throw new Error(await extractApiError(response, 'Live scan could not be stopped.'));
  }

  const payload = await response.json();

  setScanMeta(payload.scan);
  appendLogLine('Stop requested for nonstop live scan.', 'status');
}

async function initializeDashboard() {
  try {
    await fetchMatches();
    await fetchRateLimit(true);
    const scan = await fetchScanStatus();
    setScanMeta(scan);
    if (scan && isScanActive(scan)) {
      appendLogLine(`Rejoined active scan for ${scan.repo}`, 'status');
      connectToScanStream(scan.scan_id);
    }
  } catch (error) {
    appendLogLine(error.message, 'error');
  }
}

elements.scanForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const repo = elements.repoInput.value.trim();
  if (!repo) {
    return;
  }

  try {
    await startRepoScan(repo);
  } catch (error) {
    appendLogLine(error.message, 'error');
    elements.scanButton.disabled = false;
  }
});

elements.startLiveScanButton.addEventListener('click', async () => {
  try {
    await startLiveScan();
  } catch (error) {
    appendLogLine(error.message, 'error');
    updateScanControls(state.currentScan);
  }
});

elements.stopLiveScanButton.addEventListener('click', async () => {
  try {
    await stopLiveScan();
  } catch (error) {
    appendLogLine(error.message, 'error');
  }
});

elements.refreshMatchesButton.addEventListener('click', async () => {
  try {
    await fetchMatches();
    appendLogLine('Match list refreshed.', 'status');
  } catch (error) {
    appendLogLine(error.message, 'error');
  }
});

elements.clearLogButton.addEventListener('click', () => {
  clearLog();
  appendLogLine('Log cleared.', 'status');
});

elements.openSettingsButton.addEventListener('click', async () => {
  openSettingsModal();

  try {
    await ensureSettingsLoaded();
  } catch (error) {
    setSettingsStatus(error.message, 'error');
    appendLogLine(error.message, 'error');
  }
});

elements.closeSettingsButton.addEventListener('click', () => {
  closeSettingsModal();
});

elements.settingsBackdrop.addEventListener('click', () => {
  closeSettingsModal();
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && isSettingsModalOpen()) {
    closeSettingsModal();
  }
});

elements.settingsForm.addEventListener('submit', async (event) => {
  event.preventDefault();

  try {
    await saveSettings();
  } catch (error) {
    setSettingsStatus(error.message, 'error');
    appendLogLine(error.message, 'error');
  }
});

elements.reloadSettingsButton.addEventListener('click', async () => {
  try {
    await ensureSettingsLoaded(true);
    appendLogLine('Settings reloaded.', 'status');
  } catch (error) {
    setSettingsStatus(error.message, 'error');
    appendLogLine(error.message, 'error');
  }
});

elements.filterInput.addEventListener('input', renderMatches);
elements.sortSelect.addEventListener('change', renderMatches);

initializeDashboard();

window.setInterval(async () => {
  try {
    await fetchRateLimit();
    updateScanControls(state.currentScan);
  } catch {
    renderRateLimit();
  }
}, RATE_LIMIT_POLL_INTERVAL_MS);
