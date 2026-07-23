/**
 * Caging Chat — pure client-side built-in command handling.
 *
 * All ``?`` (query) and ``>`` (action) commands are intercepted and
 * handled locally.  Only plain-text messages are forwarded to the
 * ``/chat`` endpoint for AI response.
 *
 * Usage:
 *   const chat = new Chat({
 *     prefix:       getPrefix(),          // URL prefix (/admin, /cadmin, …)
 *     apiCall:      apiCall,              // function(action, params) → Promise
 *     getActiveId:  () => activeRequestId,
 *     setActiveId:  (id) => { activeRequestId = id; },
 *     getRequests:  () => allRequests,
 *     refreshFn:    refreshRequests,      // () → Promise (re-fetch request list)
 *     onStatusFilterChange: (status) => { activeStatusFilter = status; },
 *     chatInputEl:  document.getElementById('chatInput'),
 *     chatSendEl:   document.getElementById('chatSend'),
 *     chatMsgsEl:   document.getElementById('chatMessages'),
 *     chatToggleEl: document.getElementById('chatToggleBtn'),
 *     chatPanelEl:  document.getElementById('chatPanel'),
 *   });
 *   chat.init();
 */
class Chat {
  constructor(opts) {
    this.prefix        = opts.prefix        || '';
    this._apiCall      = opts.apiCall;         // function(action, params) → Promise<data>
    this._getActiveId  = opts.getActiveId;     // () → string|null
    this._setActiveId  = opts.setActiveId;     // (id) → void
    this._getRequests  = opts.getRequests;     // () → array
    this._refreshFn    = opts.refreshFn;       // () → Promise
    this._onStatusFilterChange = opts.onStatusFilterChange;  // (status) → void

    // DOM
    this._inputEl   = opts.chatInputEl;
    this._sendEl    = opts.chatSendEl;
    this._msgsEl    = opts.chatMsgsEl;
    this._toggleEl  = opts.chatToggleEl;
    this._panelEl   = opts.chatPanelEl;

    // State
    this.history    = [];     // [{role, content}, …]
    this._historyIdx = -1;    // for arrow-key recall
  }

  // ═══ Initialisation ═════════════════════════════════════════════

  init() {
    this._loadHistory();
    this._bindEvents();
    this._render();
  }

  // ═══ Event binding ══════════════════════════════════════════════

  _bindEvents() {
    this._sendEl.addEventListener('click', () => this._send());
    this._inputEl.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this._send(); }
    });
    // Arrow-key history recall
    this._inputEl.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowUp' && this._inputEl.value === '') {
        e.preventDefault();
        const msgs = this.history.filter(m => m.role === 'user');
        if (msgs.length) {
          this._historyIdx = Math.min(this._historyIdx + 1, msgs.length - 1);
          this._inputEl.value = msgs[msgs.length - 1 - this._historyIdx].content;
        }
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        const msgs = this.history.filter(m => m.role === 'user');
        this._historyIdx = Math.max(this._historyIdx - 1, -1);
        this._inputEl.value = this._historyIdx >= 0
          ? msgs[msgs.length - 1 - this._historyIdx].content : '';
      }
    });
    this._toggleEl.addEventListener('click', () => this._toggle());
  }

  // ═══ Public helpers ═════════════════════════════════════════════

  addSystemMsg(text) {
    this.history.push({ role: 'system', content: text });
    this._saveHistory();
    this._render();
  }

  addUserMsg(text) {
    this.history.push({ role: 'user', content: text });
    this._saveHistory();
  }

  addAssistantMsg(text) {
    this.history.push({ role: 'assistant', content: text });
    this._saveHistory();
  }

  clearHistory() {
    this.history = [];
    this._saveHistory();
    this._render();
  }

  // ═══ Send pipeline ══════════════════════════════════════════════

  async _send() {
    const msg = this._inputEl.value.trim();
    if (!msg) return;

    this.addUserMsg(msg);
    this._inputEl.value = '';
    this._sendEl.disabled = true;
    this._sendEl.textContent = '...';
    this._render();

    try {
      const firstChar = msg[0];
      if (firstChar === '?') {
        await this._handleQuery(msg);
      } else if (firstChar === '>') {
        await this._handleAction(msg);
      } else {
        await this._handleAIChat(msg);
      }
    } catch (e) {
      this.addSystemMsg('❌ Error: ' + e.message);
    } finally {
      this._sendEl.disabled = false;
      this._sendEl.textContent = 'Send';
      this._render();
    }
  }

  // ═══ ? Query commands (pure JS) ═════════════════════════════════

  async _handleQuery(msg) {
    const cmd = msg.slice(1).trim().toLowerCase();

    // ?help / ?h
    if (cmd === 'help' || cmd === 'h') {
      this.addAssistantMsg(
        '**Built-in commands:**\n\n' +
        '**? Queries**\n' +
        '• `?help` / `?h` — Show this help\n' +
        '• `?risk` / `?r` — AI risk analysis for active request\n' +
        '• `?detail` / `?d` — Show active request details\n' +
        '• `?[status]` — Filter by status (e.g. `?awaiting_review`)\n\n' +
        '**> Actions**\n' +
        '• `>approve` / `>a` — Approve active request\n' +
        '• `>reject` / `>j` — Reject active request\n' +
        '• `>delegate` / `>g` — Delegate active request\n' +
        '• `>clear` / `>c` — Clear chat window\n' +
        '• `>new` — Start new chat session\n' +
        '• `>approve all` — Approve all awaiting requests\n' +
        '• `>delete all` — Delete all requests'
      );
      return;
    }

    // ?risk / ?r
    if (cmd === 'risk' || cmd === 'r') {
      const id = this._getActiveId();
      if (!id) { this.addSystemMsg('❌ No active request selected. Click a request card first.'); return; }
      this.addSystemMsg('🔍 Analysing risk for ' + id + '...');
      try {
        const result = await this._apiCall('ai_risk_analysis', { request_id: id });
        this.addAssistantMsg(
          '**Risk Analysis for ' + id + '**\n\n' +
          'Risk score: **' + (result.risk_score ?? 'N/A') + '** / 100\n' +
          'Decision: **' + (result.decision ?? 'N/A') + '**\n\n' +
          (result.explanation || '')
        );
      } catch (e) {
        this.addSystemMsg('❌ Risk analysis failed: ' + e.message);
      }
      return;
    }

    // ?detail / ?d
    if (cmd === 'detail' || cmd === 'd') {
      const id = this._getActiveId();
      if (!id) { this.addSystemMsg('❌ No active request selected.'); return; }
      try {
        const detail = await this._apiCall('get_request_detail', { request_id: id });
        this.addAssistantMsg(
          '**Request: ' + id + '**\n\n' +
          '• Type: `' + (detail.type || '?') + '`\n' +
          '• Status: `' + (detail.status || '?') + '`\n' +
          '• Requester: `' + (detail.requester_id || '?') + '`\n' +
          '• Topic: `' + (detail.topic || 'na') + '`\n' +
          '• Command: `' + (detail.command || detail.payload?.command || '?') + '`\n' +
          '• Risk score: ' + (detail.risk_score ?? 'N/A') + '\n' +
          '• Review note: ' + (detail.review_note || '(none)') + '\n' +
          '• Created: ' + (detail.created_at || '?')
        );
      } catch (e) {
        this.addSystemMsg('❌ Detail fetch failed: ' + e.message);
      }
      return;
    }

    // ?[status] — status filter (e.g. ?awaiting_review, ?approved, ?rejected)
    const validStatuses = ['awaiting_review', 'approved', 'rejected', 'executing', 'completed', 'escalated', 'failed'];
    if (validStatuses.includes(cmd)) {
      if (this._onStatusFilterChange) this._onStatusFilterChange(cmd);
      // Also update the status filter dropdown if present
      const sf = document.getElementById('statusFilter');
      if (sf) sf.value = cmd;
      this.addSystemMsg('📋 Filtering by status: **' + cmd + '**');
      if (this._refreshFn) {
        try { await this._refreshFn(); } catch (e) { /* ignore */ }
      }
      return;
    }

    // Unknown ? command
    this.addSystemMsg('❌ Unknown query: `' + msg + '`. Type `?help` for available commands.');
  }

  // ═══ > Action commands (pure JS) ════════════════════════════════

  async _handleAction(msg) {
    const cmd = msg.slice(1).trim().toLowerCase();

    // >approve / >a
    if (cmd === 'approve' || cmd === 'a') {
      const id = this._getActiveId();
      if (!id) { this.addSystemMsg('❌ No active request selected.'); return; }
      this.addSystemMsg('⚡ Approving ' + id + '...');
      try {
        await this._apiCall('approve_request', { request_id: id, status: 'approved' });
        this.addSystemMsg('✅ Approved: ' + id);
        if (this._refreshFn) await this._refreshFn();
      } catch (e) { this.addSystemMsg('❌ Approve failed: ' + e.message); }
      return;
    }

    // >reject / >j
    if (cmd === 'reject' || cmd === 'j') {
      const id = this._getActiveId();
      if (!id) { this.addSystemMsg('❌ No active request selected.'); return; }
      this.addSystemMsg('⚡ Rejecting ' + id + '...');
      try {
        await this._apiCall('approve_request', { request_id: id, status: 'rejected' });
        this.addSystemMsg('🚫 Rejected: ' + id);
        if (this._refreshFn) await this._refreshFn();
      } catch (e) { this.addSystemMsg('❌ Reject failed: ' + e.message); }
      return;
    }

    // >delegate / >g
    if (cmd === 'delegate' || cmd === 'g') {
      const id = this._getActiveId();
      if (!id) { this.addSystemMsg('❌ No active request selected.'); return; }
      // Prompt for reviewer username
      const target = prompt('Enter reviewer username to delegate to:');
      if (!target) { this.addSystemMsg('Delegation cancelled.'); return; }
      this.addSystemMsg('⚡ Delegating ' + id + ' to ' + target + '...');
      try {
        await this._apiCall('approve_request', { request_id: id, delegate_to: target });
        this.addSystemMsg('✅ Delegated ' + id + ' to **' + target + '**');
        if (this._refreshFn) await this._refreshFn();
      } catch (e) { this.addSystemMsg('❌ Delegate failed: ' + e.message); }
      return;
    }

    // >clear / >c
    if (cmd === 'clear' || cmd === 'c') {
      this.clearHistory();
      return;
    }

    // >new
    if (cmd === 'new') {
      this.clearHistory();
      this.addSystemMsg('✨ New chat session started.');
      return;
    }

    // >approve all
    if (cmd === 'approve all') {
      this.addSystemMsg('⚡ Approving all awaiting requests...');
      try {
        const result = await this._apiCall('approve_all', {});
        this.addSystemMsg('✅ Done: ' + JSON.stringify(result).substring(0, 200));
        if (this._refreshFn) await this._refreshFn();
      } catch (e) { this.addSystemMsg('❌ Approve all failed: ' + e.message); }
      return;
    }

    // >delete all
    if (cmd === 'delete all') {
      if (!confirm('Delete ALL requests? This cannot be undone.')) {
        this.addSystemMsg('Delete all cancelled.');
        return;
      }
      this.addSystemMsg('⚡ Deleting all requests...');
      try {
        const result = await this._apiCall('delete_all', {});
        this.addSystemMsg('✅ Done: ' + JSON.stringify(result).substring(0, 200));
        if (this._refreshFn) await this._refreshFn();
      } catch (e) { this.addSystemMsg('❌ Delete all failed: ' + e.message); }
      return;
    }

    // Unknown > command
    this.addSystemMsg('❌ Unknown action: `' + msg + '`. Type `?help` for available commands.');
  }

  // ═══ AI Chat (forward to /chat endpoint) ════════════════════════

  async _handleAIChat(msg) {
    const id = this._getActiveId();
    let context = '';

    // Build context from active request if available
    if (id) {
      const reqs = this._getRequests ? this._getRequests() : [];
      const req = reqs.find(r => r.id === id);
      if (req) {
        const payload = req.payload || {};
        context = JSON.stringify({
          request_id: req.id,
          type: req.type,
          status: req.status,
          requester: req.requester_id,
          topic: req.topic,
          command: typeof payload === 'object' ? (payload.command || '') : '',
          risk_score: req.risk_score,
        });
      }
    }

    try {
      const resp = await fetch(this.prefix + '/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: msg,
          chat_history: this.history.slice(-20),
          context: context,
        }),
      });

      if (resp.status === 401) {
        window.location.href = this.prefix + '/ui/login';
        return;
      }

      const data = await resp.json();
      if (data.error) {
        this.addSystemMsg('❌ ' + data.error);
      } else {
        this.addAssistantMsg(data.message || data.response || 'No response.');
      }
    } catch (e) {
      this.addSystemMsg('❌ AI chat error: ' + e.message);
    }
  }

  // ═══ History persistence ════════════════════════════════════════

  _saveHistory() {
    try {
      localStorage.setItem('caging_chat_history', JSON.stringify(this.history.slice(-50)));
    } catch (_) { /* quota exceeded, ignore */ }
  }

  _loadHistory() {
    try {
      const raw = localStorage.getItem('caging_chat_history');
      if (raw) this.history = JSON.parse(raw);
    } catch (_) { this.history = []; }
  }

  // ═══ Rendering ══════════════════════════════════════════════════

  _render() {
    if (!this._msgsEl) return;
    this._msgsEl.innerHTML = this.history.map(m => {
      const role = m.role;
      let content = this._escapeHtml(m.content || '');
      if (role === 'assistant') {
        content = this._formatAssistant(content);
      }
      return '<div class="chat-msg ' + role + '">' + content + '</div>';
    }).join('');
    this._msgsEl.scrollTop = this._msgsEl.scrollHeight;
  }

  _escapeHtml(s) {
    if (!s) return '';
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  _formatAssistant(text) {
    // Simple markdown-like: code blocks and newlines
    let out = text;
    out = out.replace(/```([\s\S]*?)```/g, '<pre>$1</pre>');
    out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
    out = out.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/\n/g, '<br>');
    return out;
  }

  // ═══ Panel toggle ═══════════════════════════════════════════════

  _toggle() {
    if (!this._panelEl || !this._toggleEl) return;
    this._panelEl.classList.toggle('collapsed');
    this._toggleEl.textContent = this._panelEl.classList.contains('collapsed') ? '▶' : '◀';
  }
}
