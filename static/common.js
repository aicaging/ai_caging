/**
 * Caging UI Common Utilities
 * Cookie-based session — no manual token management.
 */

// ════════════════════════════════════════════════════════════════════
// Site Prefix
// ════════════════════════════════════════════════════════════════════

function getPrefix() {
    const pn = location.pathname;
    for (const p of ['/admin', '/root', '/cadmin']) {
        if (pn.startsWith(p + '/') || pn === p) return p;
    }
    return '';
}

// ════════════════════════════════════════════════════════════════════
// Navigation
// ════════════════════════════════════════════════════════════════════

/** Navigate to a path via unified /ui/api endpoint.
 *  Only login page uses direct URL; all other pages load through the API. */
async function navigateTo(path) {
    const prefix = getPrefix();

    // Login page stays as direct endpoint
    if (path === 'login') {
        window.location.href = prefix + '/ui/login';
        return;
    }

    // Map path to API action
    let action, params = {};
    if (path === 'dashboard') {
        action = 'get_dashboard_page';
    } else if (path.startsWith('request/')) {
        action = 'get_request_detail_page';
        params.request_id = path.split('/')[1];
    } else {
        throw new Error('Unknown navigation path: ' + path);
    }

    // Call the unified API — cookie sent automatically
    const resp = await fetch(prefix + '/ui/api', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, params }),
    });

    if (resp.status === 401) {
        window.location.href = prefix + '/ui/login';
        return;
    }

    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'Navigation failed');

    // Update browser URL without reloading
    const newUrl = prefix + '/ui/' + path;
    history.pushState(null, '', newUrl);

    // Replace entire page with returned HTML — scripts re-execute automatically
    document.open();
    document.write(data.data.html);
    document.close();
}

/** Build WebSocket URL — cookie sent with handshake, no token needed */
function wsUrl(path) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const prefix = getPrefix();
    return proto + '//' + location.host + prefix + path;
}

// ════════════════════════════════════════════════════════════════════
// Unified API
// ════════════════════════════════════════════════════════════════════

/**
 * Single entry point for all UI data operations.
 * Sends POST {prefix}/ui/api with { action, params }.
 * Cookie sent automatically — no Authorization header needed.
 * Returns parsed response.data on success, throws on error.
 */
async function apiCall(action, params = {}) {
    const prefix = getPrefix();
    const resp = await fetch(prefix + '/ui/api', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, params }),
    });
    if (resp.status === 401) {
        window.location.href = prefix + '/ui/login';
        throw new Error('Unauthorized');
    }
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || data.detail || 'API error');
    return data.data !== undefined ? data.data : data;
}

/** Logout: server clears cookie, then redirect */
function logout() {
    apiCall('logout').catch(() => {});
    window.location.href = getPrefix() + '/ui/login';
}