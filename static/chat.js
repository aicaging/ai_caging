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
        '• `?p` / `?policy` — Open policy rule editor for active request\n' +
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

    // ?p / ?policy — open policy rule editor for current request
    if (cmd === 'p' || cmd === 'policy') {
      const id = this._getActiveId();
      if (!id) { this.addSystemMsg('❌ No active request selected. Click a request card first.'); return; }
      await this._showPolicyDialog(id);
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

  // ═══ Policy Dialog ══════════════════════════════════════════════

  async _showPolicyDialog(requestId) {
    this.addSystemMsg('📋 Loading policy for ' + requestId + '...');

    let data;
    try {
      data = await this._apiCall('get_policy_for_request', { request_id: requestId });
    } catch (e) {
      this.addSystemMsg('❌ Failed to load policy: ' + e.message);
      return;
    }

    const { matched_rule, all_rules, request_info } = data;
    const self = this;

    // Remove any existing dialog
    const existing = document.getElementById('policyDialogOverlay');
    if (existing) existing.remove();

    // Build overlay
    const overlay = document.createElement('div');
    overlay.id = 'policyDialogOverlay';
    overlay.className = 'policy-dialog-overlay';
    overlay.innerHTML = this._buildPolicyDialogHTML(matched_rule, all_rules, request_info);

    document.body.appendChild(overlay);

    // Cleanup helper — remove overlay + unbind ESC
    const cleanup = () => {
      overlay.remove();
      document.removeEventListener('keydown', escHandler);
    };

    // Close on overlay click
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) cleanup();
    });

    // Close on Esc
    const escHandler = (e) => { if (e.key === 'Escape') cleanup(); };
    document.addEventListener('keydown', escHandler);

    // Close buttons (✕ and Cancel)
    overlay.querySelectorAll('.policy-dialog-close').forEach(el => el.addEventListener('click', cleanup));

    // Rule selector change → populate form + toggle buttons
    const footer = overlay.querySelector('.policy-dialog-footer');
    const ruleSelect = overlay.querySelector('#policyRuleSelect');
    if (ruleSelect) {
      const toggleButtons = () => {
        if (ruleSelect.value === '') {
          footer.classList.remove('has-rule');
        } else {
          footer.classList.add('has-rule');
        }
      };
      ruleSelect.addEventListener('change', () => {
        const idx = ruleSelect.value;
        if (idx === '') {
          // Clear form for new rule
          self._resetPolicyForm(overlay, request_info);
        } else {
          const rule = all_rules[parseInt(idx)];
          if (rule) self._populatePolicyForm(overlay, rule);
        }
        toggleButtons();
      });
      // Trigger change to populate with matched/default + set initial button state
      ruleSelect.dispatchEvent(new Event('change'));
    }

    // Save (Update) button
    overlay.querySelector('#policyBtnUpdate').addEventListener('click', async () => {
      const form = self._readPolicyForm(overlay);
      const selIdx = ruleSelect.value;
      if (selIdx === '') {
        self.addSystemMsg('❌ Select an existing rule to update, or use "Add as New".');
        return;
      }
      const rule = all_rules[parseInt(selIdx)];
      if (!rule) return;
      try {
        await self._apiCall('update_policy', { rule_id: rule.id, ...form });
        self.addSystemMsg('✅ Policy rule #' + rule.id + ' updated.');
        cleanup();
      } catch (e) {
        self.addSystemMsg('❌ Update failed: ' + e.message);
      }
    });

    // Delete button
    overlay.querySelector('#policyBtnDelete').addEventListener('click', async () => {
      const selIdx = ruleSelect.value;
      if (selIdx === '') return;
      const rule = all_rules[parseInt(selIdx)];
      if (!rule) return;
      if (!confirm('Delete policy rule "' + rule.name + '" (# ' + rule.id + ')? This cannot be undone.')) return;
      try {
        await self._apiCall('delete_policy', { rule_id: rule.id });
        self.addSystemMsg('🗑 Policy rule #' + rule.id + ' deleted.');
        cleanup();
      } catch (e) {
        self.addSystemMsg('❌ Delete failed: ' + e.message);
      }
    });

    // Add as New button
    overlay.querySelector('#policyBtnAdd').addEventListener('click', async () => {
      const form = self._readPolicyForm(overlay);
      if (!form.name.trim()) {
        self.addSystemMsg('❌ Rule name is required.');
        return;
      }
      try {
        const result = await self._apiCall('add_policy', form);
        self.addSystemMsg('✅ New policy rule created: #' + (result.rule?.id || '?'));
        cleanup();
      } catch (e) {
        self.addSystemMsg('❌ Add failed: ' + e.message);
      }
    });
  }

  _buildPolicyDialogHTML(matched_rule, all_rules, request_info) {
    const cmd = request_info.command || request_info.base_command || '(none)';
    const topic = request_info.topic || '(none)';
    const matchedName = matched_rule.rule_name || 'default';

    let ruleOptions = '<option value="">-- New Rule (based on request) --</option>';
    let matchedIdx = '';
    (all_rules || []).forEach((r, i) => {
      const sel = r.name === matchedName ? ' selected' : '';
      if (r.name === matchedName) matchedIdx = String(i);
      ruleOptions += '<option value="' + i + '"' + sel + '>' + this._escapeHtml(r.name) + ' (action: ' + this._escapeHtml(r.action) + ', pri: ' + (r.priority ?? 100) + ')</option>';
    });

    // Determine if we have a matched real rule (not "default")
    const hasMatch = matched_rule && matched_rule.action !== undefined && matchedName !== 'default';
    const footerClass = hasMatch ? ' has-rule' : '';

    return '' +
      '<div class="policy-dialog">' +
        '<div class="policy-dialog-header">' +
          '<h3>📜 Policy Rule Editor</h3>' +
          '<button class="policy-dialog-close" title="Close">✕</button>' +
        '</div>' +
        '<div class="policy-dialog-body">' +
          '<div class="policy-info-bar">' +
            '<span><strong>Request:</strong> <code>' + this._escapeHtml(cmd) + '</code></span>' +
            '<span><strong>Topic:</strong> <code>' + this._escapeHtml(topic) + '</code></span>' +
            '<span><strong>Matched:</strong> <em>' + this._escapeHtml(matchedName) + ' → ' + this._escapeHtml(matched_rule.action || '?') + '</em></span>' +
          '</div>' +
          '<div class="policy-form-group">' +
            '<label for="policyRuleSelect">Existing Rule</label>' +
            '<select id="policyRuleSelect">' + ruleOptions + '</select>' +
          '</div>' +
          '<div class="policy-form-group">' +
            '<label for="policyName">Rule Name *</label>' +
            '<input type="text" id="policyName" placeholder="e.g. block_rm_for_guests" />' +
          '</div>' +
          '<div class="policy-form-row">' +
            '<div class="policy-form-group" style="flex:2">' +
              '<label for="policyCondition">Condition (Python expr)</label>' +
              '<input type="text" id="policyCondition" placeholder="e.g. base_command == \'rm\'" />' +
            '</div>' +
            '<div class="policy-form-group" style="flex:1">' +
              '<label for="policyAction">Action</label>' +
              '<select id="policyAction">' +
                '<option value="allow">allow</option>' +
                '<option value="require_human" selected>require_human</option>' +
                '<option value="escalate">escalate</option>' +
                '<option value="deny">deny</option>' +
                '<option value="ai">ai</option>' +
                '<option value="ai_screen">ai_screen</option>' +
              '</select>' +
            '</div>' +
          '</div>' +
          '<div class="policy-form-group">' +
            '<label for="policyReason">Reason</label>' +
            '<input type="text" id="policyReason" placeholder="Why this rule exists" />' +
          '</div>' +
          '<div class="policy-form-row">' +
            '<div class="policy-form-group" style="flex:1">' +
              '<label for="policyPriority">Priority</label>' +
              '<input type="number" id="policyPriority" value="100" min="1" max="999" />' +
            '</div>' +
            '<div class="policy-form-group policy-form-check">' +
              '<label><input type="checkbox" id="policyDualApproval" /> Dual Approval</label>' +
            '</div>' +
            '<div class="policy-form-group policy-form-check">' +
              '<label><input type="checkbox" id="policyEnabled" checked /> Enabled</label>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="policy-dialog-footer' + footerClass + '">' +
          '<button class="btn-policy btn-update" id="policyBtnUpdate">💾 Update Rule</button>' +
          '<button class="btn-policy btn-delete" id="policyBtnDelete">🗑 Delete Rule</button>' +
          '<button class="btn-policy btn-add" id="policyBtnAdd">➕ Add as New</button>' +
          '<button class="btn-policy btn-cancel policy-dialog-close">Cancel</button>' +
        '</div>' +
      '</div>';
  }

  _populatePolicyForm(overlay, rule) {
    overlay.querySelector('#policyName').value = rule.name || '';
    overlay.querySelector('#policyCondition').value = rule.condition || 'true';
    overlay.querySelector('#policyAction').value = rule.rule_action || rule.action || 'require_human';
    overlay.querySelector('#policyReason').value = rule.reason || '';
    overlay.querySelector('#policyPriority').value = rule.priority ?? 100;
    overlay.querySelector('#policyDualApproval').checked = !!rule.dual_approval;
    overlay.querySelector('#policyEnabled').checked = rule.enabled !== false;
  }

  _resetPolicyForm(overlay, request_info) {
    const cmd = request_info.command || '';
    const base = request_info.base_command || cmd;
    overlay.querySelector('#policyName').value = base ? 'rule_for_' + base : '';
    overlay.querySelector('#policyCondition').value = base ? ("base_command == '" + base.replace(/'/g, "\\'") + "'") : 'true';
    overlay.querySelector('#policyAction').value = 'require_human';
    overlay.querySelector('#policyReason').value = '';
    overlay.querySelector('#policyPriority').value = 100;
    overlay.querySelector('#policyDualApproval').checked = false;
    overlay.querySelector('#policyEnabled').checked = true;
  }

  _readPolicyForm(overlay) {
    return {
      name: overlay.querySelector('#policyName').value.trim(),
      condition: overlay.querySelector('#policyCondition').value.trim() || 'true',
      action: overlay.querySelector('#policyAction').value,
      reason: overlay.querySelector('#policyReason').value.trim(),
      priority: parseInt(overlay.querySelector('#policyPriority').value) || 100,
      dual_approval: overlay.querySelector('#policyDualApproval').checked,
      enabled: overlay.querySelector('#policyEnabled').checked,
    };
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
