/**
 * Caging User Dashboard — request cards, WebSocket, AI chat panel.
 */
(function() {
'use strict';

const prefix = getPrefix();
let currentUser = null;
let activeRequestId = null;
let activeStatusFilter = 'awaiting_review';
let allRequests = [];
let chat = null;
let ws = null;

// ── Init on DOM ready ──
document.addEventListener('DOMContentLoaded', async () => {
  try {
    currentUser = await apiCall('get_current_user');
  } catch (e) {
    window.location.href = prefix + '/ui/login';
    return;
  }
  renderHeader();
  renderStatusBar();
  setupChatPanel();
  await refreshRequests();
  connectWS();
});

// ═══ Header ═══
function renderHeader() {
  const roleClass = 'badge-' + (currentUser.role === 'admin' ? 'admin' :
                    currentUser.role === 'reviewer' ? 'reviewer' : 'user');
  document.getElementById('header').innerHTML =
    '<div class="header-left">' +
      '<h1>🔐 Caging</h1>' +
      '<span class="badge ' + roleClass + '">' + currentUser.role + '</span>' +
    '</div>' +
    '<div class="header-right">' +
      '<span class="user-info"><strong>' + escapeHtml(currentUser.id) + '</strong></span>' +
      '<button class="btn-icon logout" onclick="logout()">🚪 Sign Out</button>' +
    '</div>';
}

// ═══ Status Bar ═══
function renderStatusBar() {
  const sb = document.querySelector('.right-panel .statusbar');
  sb.innerHTML =
    '<span class="status-label">Filter:</span>' +
    '<select id="statusFilter">' +
      '<option value="">All Requests</option>' +
      '<option value="pending">Pending</option>' +
      '<option value="awaiting_review">Awaiting Review</option>' +
      '<option value="first_approved">First Approved</option>' +
      '<option value="approved">Approved</option>' +
      '<option value="rejected">Rejected</option>' +
      '<option value="completed">Completed</option>' +
      '<option value="failed">Failed</option>' +
      '<option value="escalated">Escalated</option>' +
    '</select>' +
    '<span class="count-badge" id="requestCount"></span>';
  document.getElementById('statusFilter').addEventListener('change', function() {
    activeStatusFilter = this.value;
    activeRequestId = null;
    refreshRequests();
  });
  document.getElementById('statusFilter').value = activeStatusFilter;
}

// ═══ Request Cards — Window Style ═══
function renderCards() {
  const panel = document.getElementById('requestPanel');
  document.getElementById('requestCount').textContent = allRequests.length + ' requests';
  if (!allRequests.length) {
    panel.innerHTML = '<div class="empty-state"><div class="icon">📋</div><h3>No requests</h3><p>Requests will appear here when submitted.</p></div>';
    return;
  }
  panel.innerHTML = allRequests.map(r => {
    const activeCls = r.id === activeRequestId ? ' active' : '';
    const statusCls = 'status-' + (r.status || 'pending');
    const typeCls = 'type-' + (r.type || 'command');
    const riskCls = r.risk_score > 7 ? 'risk-high' : r.risk_score > 3 ? 'risk-medium' : 'risk-low';
    const scriptContent = getScriptContent(r);
    const age = r.created_at ? timeAgo(r.created_at) : '';
    return '<div class="request-card' + activeCls + '" data-id="' + escapeHtml(r.id) + '">' +
      '<div class="card-titlebar">' +
        '<span class="card-type ' + typeCls + '">' + escapeHtml(r.type || '?') + '</span>' +
        '<span class="win-title">' + escapeHtml(r.topic || 'Untitled') + '</span>' +
        '<span class="win-id">#' + escapeHtml((r.id || '').substring(0, 18)) + '</span>' +
        '<span class="win-dot min" title="Minimize">🗕</span>' +
        '<span class="win-dot max" title="Maximize">🗖</span>' +
        '<label class="deselect-cb" title="Select/Deselect"><input type="checkbox"' + (r.id === activeRequestId ? ' checked' : '') + '></label>' +
      '</div>' +
      '<div class="card-body">' +
        '<div class="card-meta">' +
          '<span><span class="status-dot ' + statusCls + '"></span>' + escapeHtml(r.status || 'pending') + '</span>' +
          (r.risk_score != null ? '<span class="' + riskCls + '">Risk: ' + r.risk_score + '</span>' : '') +
          '<span>' + escapeHtml(r.requester_id || '') + '</span>' +
          (age ? '<span>' + age + '</span>' : '') +
        '</div>' +
        (scriptContent ? '<pre class="card-script">' + escapeHtml(scriptContent) + '</pre>' : '') +
        '<div class="card-audit"></div>' +
      '</div>' +
    '</div>';
  }).join('');

  // Click handlers
  panel.querySelectorAll('.request-card').forEach(card => {
    const rid = card.dataset.id;
    // titlebar click → select
    card.querySelector('.card-titlebar').addEventListener('click', () => {
      activeRequestId = rid;
      renderCards();
    });
    // min dot → minimize (collapse)
    card.querySelector('.win-dot.min').addEventListener('click', (e) => {
      e.stopPropagation();
      card.classList.remove('maximized');
      card.classList.toggle('minimized');
    });
    // max dot → maximize (expand, fetch audit)
    card.querySelector('.win-dot.max').addEventListener('click', async (e) => {
      e.stopPropagation();
      card.classList.remove('minimized');
      const wasMaximized = card.classList.contains('maximized');
      if (wasMaximized) {
        card.classList.remove('maximized');
        return;
      }
      card.classList.add('maximized');
      // Fetch audit history if not yet loaded
      const auditEl = card.querySelector('.card-audit');
      if (auditEl && !auditEl.dataset.loaded) {
        auditEl.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
        try {
          const detail = await apiCall('get_request_detail', { request_id: rid });
          const audit = detail.audit || [];
          if (audit.length === 0) {
            auditEl.innerHTML = '<div class="audit-empty">No audit entries</div>';
          } else {
            auditEl.innerHTML = '<div class="audit-header">📋 Audit History</div>' +
              audit.map(a => '<div class="audit-item">' +
                '<span class="audit-action">' + escapeHtml(a.action || '') + '</span>' +
                '<span class="audit-actor">' + escapeHtml(a.actor_id || '') + '</span>' +
                (a.detail ? '<span class="audit-detail">' + escapeHtml(a.detail) + '</span>' : '') +
                (a.created_at ? '<span class="audit-time">' + timeAgo(a.created_at) + '</span>' : '') +
              '</div>').join('');
          }
          auditEl.dataset.loaded = '1';
        } catch (err) {
          auditEl.innerHTML = '<div class="audit-error">Failed to load audit: ' + escapeHtml(err.message) + '</div>';
        }
      }
    });
    // deselect checkbox → toggle selection
    card.querySelector('.deselect-cb input').addEventListener('change', (e) => {
      e.stopPropagation();
      if (e.target.checked) {
        activeRequestId = rid;
      } else {
        if (activeRequestId === rid) { activeRequestId = null; }
      }
      renderCards();
    });
  });
}

function getScriptContent(r) {
  const p = r.payload;
  if (!p) return '';
  if (typeof p === 'string') {
    try { return JSON.parse(p).script_source || JSON.parse(p).command || ''; } catch(_) { return p.substring(0, 2000); }
  }
  return p.script_source || p.command || '';
}

function timeAgo(ts) {
  const s = Math.floor((Date.now() - new Date(ts + 'Z').getTime()) / 1000);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}

async function refreshRequests() {
  const panel = document.getElementById('requestPanel');
  panel.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  try {
    const data = await apiCall('list_requests', { status: activeStatusFilter || undefined });
    allRequests = data || [];
    renderCards();
  } catch (e) {
    panel.innerHTML = '<div class="empty-state"><div class="icon">⚠️</div><h3>Failed to load requests</h3><p>' + escapeHtml(e.message) + '</p></div>';
  }
}

// ═══ WebSocket ═══
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = proto + '//' + location.host + prefix + '/ws/notifications';
  try {
    ws = new WebSocket(wsUrl);
    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data.type === 'heartbeat') return;
        if (data.type === 'request_update') {
          showToast('🔄 ' + (data.status || 'update') + ' — #' + (data.request_id || '').substring(0, 8));
          refreshRequests();
        } else {
          showToast('📢 ' + JSON.stringify(data).substring(0, 80));
        }
      } catch (_) {}
    };
    ws.onclose = () => { setTimeout(connectWS, 5000); };
    ws.onerror = () => { ws?.close(); };
    // Ping every 30s
    setInterval(() => { if (ws?.readyState === WebSocket.OPEN) ws.send('ping'); }, 30000);
  } catch (_) {}
}

function showToast(msg) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

// ═══ Chat Panel (delegated to Chat class in chat.js) ═══
function setupChatPanel() {
  chat = new Chat({
    prefix:       prefix,
    apiCall:      apiCall,
    getActiveId:  () => activeRequestId,
    setActiveId:  (id) => { activeRequestId = id; },
    getRequests:  () => allRequests,
    refreshFn:    refreshRequests,
    onStatusFilterChange: (status) => { activeStatusFilter = status; },
    chatInputEl:  document.getElementById('chatInput'),
    chatSendEl:   document.getElementById('chatSend'),
    chatMsgsEl:   document.getElementById('chatMessages'),
    chatToggleEl: document.getElementById('chatToggleBtn'),
    chatPanelEl:  document.getElementById('chatPanel'),
  });
  chat.init();
}

function escapeHtml(s) {
  if (!s) return '';
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

})();
