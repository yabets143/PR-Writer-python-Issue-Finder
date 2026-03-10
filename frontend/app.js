const state = {
  allMatches: [],
  displayedMatches: [],
  currentScan: null,
  currentScanId: null,
  currentRepo: null,
  eventSource: null,
};

const elements = {
  totalMatches: document.getElementById('totalMatches'),
  displayedMatches: document.getElementById('displayedMatches'),
  scanStatus: document.getElementById('scanStatus'),
  activeRepo: document.getElementById('activeRepo'),
  newMatchesCount: document.getElementById('newMatchesCount'),
  scanId: document.getElementById('scanId'),
  repoInput: document.getElementById('repoInput'),
  scanButton: document.getElementById('scanButton'),
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
};

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

  elements.scanButton.disabled = active;
  elements.repoInput.disabled = active;
  elements.startLiveScanButton.disabled = active;
  elements.stopLiveScanButton.disabled = !(isContinuous && canStop);
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

elements.filterInput.addEventListener('input', renderMatches);
elements.sortSelect.addEventListener('change', renderMatches);

initializeDashboard();
