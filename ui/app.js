/**
 * LoL Dodge Tracker — frontend
 *
 * Talks to Python via window.pywebview.api.*  (returns Promises).
 * Falls back to MOCK_STATE when opened directly in a browser, so
 * you can paste index.html + style.css into Claude design and preview
 * without running Python.
 */

'use strict';

// ── Mock state for browser preview ────────────────────────────────────────────
const MOCK_STATE = {
  configured: true,
  platform: 'NA1',
  api_key_set: true,
  refresh_sec: 30,
  countdown: 18,
  active_count: 2,
  safe_count: 3,
  unchecked_count: 0,
  max_players: 20,
  teams: [
    {
      name: 'Team Alpha',
      members: ['Faker#KR1', 'Uzi#KR2'],
      status: 'TEAM QUEUING',
      status_detail: 'Played together · 2/2 active',
    },
  ],
  players: [
    { riot_id: 'Faker#KR1',  status: 'QUEUING LIKELY',   status_detail: 'Ended 3m ago (W, 28m) · 2 games this hour',  last_checked_ts: Date.now()/1000 - 10 },
    { riot_id: 'Uzi#KR2',    status: 'RECENTLY DONE',    status_detail: 'Ended 14m ago (L, 22m) · 1 game this hour',  last_checked_ts: Date.now()/1000 - 10 },
    { riot_id: 'Caps#EUW',   status: 'SAFE',             status_detail: 'No games in the last hour',                  last_checked_ts: Date.now()/1000 - 10 },
    { riot_id: 'Rekkles#EUW',status: 'PLAYED THIS HOUR', status_detail: 'Ended 40m ago (W, 35m) · 1 game this hour', last_checked_ts: Date.now()/1000 - 10 },
    { riot_id: 'Scout#LPL',  status: 'SAFE',             status_detail: 'No games in the last hour',                  last_checked_ts: Date.now()/1000 - 60 },
  ],
};

// ── Status → CSS class + dot glyph ────────────────────────────────────────────
const STATUS_CLASS = {
  'IN GAME':          's-ingame',
  'QUEUING LIKELY':   's-queuing',
  'RECENTLY DONE':    's-recent',
  'PLAYED THIS HOUR': 's-hour',
  'SAFE':             's-safe',
  'ERROR':            's-error',
  'CHECKING':         's-checking',
  'UNKNOWN':          's-checking',
  'DISABLED':         's-disabled',
  'TEAM IN GAME':     't-ingame',
  'TEAM QUEUING':     't-queuing',
  'TEAM RECENT':      't-recent',
  'TEAM PARTIAL':     't-partial',
  'TEAM SAFE':        't-safe',
};

const STATUS_DOT = {
  'IN GAME':          '▶',
  'QUEUING LIKELY':   '●',
  'RECENTLY DONE':    '●',
  'PLAYED THIS HOUR': '◐',
  'SAFE':             '○',
  'ERROR':            '✕',
  'CHECKING':         '·',
  'UNKNOWN':          '·',
  'DISABLED':         '⏸',
  'TEAM IN GAME':     '▶',
  'TEAM QUEUING':     '▲',
  'TEAM RECENT':      '◐',
  'TEAM PARTIAL':     '◑',
  'TEAM SAFE':        '○',
};

// ── API bridge ────────────────────────────────────────────────────────────────
// IMPORTANT: window.pywebview is not present at script load — it's attached
// asynchronously.  Check dynamically every call, never cache the result.
function isBrowser() {
  return !(window.pywebview && window.pywebview.api);
}

let platforms = ['NA1','BR1','LA1','LA2','EUW1','EUN1','TR1','RU','KR','JP1','OC1'];

async function api(method, ...args) {
  if (isBrowser()) {
    // Mock responses for standalone browser preview
    return { ok: true };
  }
  return window.pywebview.api[method](...args);
}

async function getState() {
  if (isBrowser()) return { ...MOCK_STATE };
  return window.pywebview.api.get_state();
}

// ── App state ─────────────────────────────────────────────────────────────────
let state = null;
let selectedId = null;       // riot_id or team name
let selectedKind = null;     // 'player' | 'team'
let dragRiotId = null;
let dragOriginTeam = null;
let collapsedTeams = new Set();
// Player rows that are currently showing the live-game participants panel.
// Only meaningful while the player is IN GAME or LOADING — pruned on every
// state refresh so the panel auto-collapses when the game ends.
let expandedRows = new Set();
const LIVE_STATES = new Set(['IN GAME']);
let pollInterval = null;

// Caches the "structural signature" of the last full render so we can skip
// the innerHTML rebuild when nothing visually meaningful has changed.
// This is the critical fix for the flickering/flashing bug: the 1 Hz poll
// used to rebuild the entire player-list DOM every second, which restarted
// every CSS animation (alert-wiggle, alert-breathe, lp-fade-in, …) and
// made the live-game dropdown flash. Now we only rebuild when structure
// actually changes, and tick-only fields (countdowns, live elapsed time)
// are updated in place.
let _lastSignature = null;

// ── DOM refs ──────────────────────────────────────────────────────────────────
const $list        = document.getElementById('player-list');
const $empty       = document.getElementById('empty-state');
const $badge       = document.getElementById('platform-badge');
const $headerMeta  = document.getElementById('header-meta');
const $footerStats = document.getElementById('footer-stats');
const $ctxMenu     = document.getElementById('context-menu');
const $dragGhost   = document.getElementById('drag-ghost');
const $toast       = document.getElementById('toast');

// ── Boot ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  populatePlatformSelect();
  bindButtons();
  bindModalClose();
  installDragHandlers();

  // If pywebview isn't ready yet, wait for it — otherwise go now
  if (window.pywebview && window.pywebview.api) {
    boot();
  } else {
    window.addEventListener('pywebviewready', boot);
    // Also fall through to mock mode after a grace period in case we're
    // genuinely in a browser (no pywebview will ever appear).
    setTimeout(() => {
      if (isBrowser()) { render(MOCK_STATE); startPolling(); }
    }, 1500);
  }
});

async function boot() {
  if (!isBrowser()) {
    try { platforms = await window.pywebview.api.get_platforms(); } catch (e) {}
    populatePlatformSelect();
  }
  startPolling();
}

function startPolling() {
  poll();
  pollInterval = setInterval(poll, 1000);
}

async function poll() {
  try {
    const s = await getState();
    if (s) render(s);
  } catch (e) {
    // silently ignore transient errors between pywebview and Python
  }
}

// ── Render ────────────────────────────────────────────────────────────────────
// Always-cheap header + footer updates.
function updateChrome(s) {
  $badge.textContent = s.platform || '—';
  $headerMeta.textContent = `${s.players.length} / ${s.max_players} players`;

  const parts = [];
  if (!s.configured)     parts.push('not configured');
  if (s.active_count)    parts.push(`${s.active_count} active`);
  if (s.safe_count)      parts.push(`${s.safe_count} safe`);
  if (s.unchecked_count) parts.push(`${s.unchecked_count} unchecked`);
  $footerStats.textContent = parts.length ? parts.join('  ·  ') : 'No players tracked';
}

// A compact string fingerprint of everything that would require a DOM
// rebuild. Tick-only fields (next_check_sec, live elapsed time) are
// deliberately excluded; those are updated in place by updateTickFields().
function structuralSignature(s) {
  const pParts = s.players.map(p => {
    const champSig = (Array.isArray(p.live_participants) && p.live_participants.length)
      ? p.live_participants.map(x => x.champion_id || 0).join(',')
      : '';
    // For statuses whose detail ticks client-side (IN GAME live timer,
    // QUEUING LIKELY / RECENTLY DONE / PLAYED THIS HOUR "Ended Xm ago")
    // we exclude the backend's string from the signature so the drifting
    // number doesn't force a full DOM rebuild on every poll — the tick
    // path rewrites the text in place instead.
    const detailTicksClientSide =
      p.status === 'IN GAME' || ENDED_STATUSES.has(p.status);
    const detailPart = detailTicksClientSide ? '' : (p.status_detail || '');
    return [
      p.riot_id, p.status, p.last_mode || '',
      p.enabled === false ? 0 : 1,
      p.my_team_id == null ? '' : p.my_team_id,
      champSig, detailPart,
    ].join('|');
  }).join(';');

  const tParts = s.teams.map(t =>
    [t.name, t.members.join(','), t.status,
     t.status_detail, t.last_mode || ''].join('|')
  ).join(';');

  return [
    pParts, '#', tParts,
    '#', [...expandedRows].sort().join(','),
    '#', [...collapsedTeams].sort().join(','),
    '#', selectedKind || '', ':', selectedId || '',
    '#', s.platform || '', s.configured ? 1 : 0, s.max_players,
  ].join('');
}

// Update only the fields that tick every second — countdowns and the
// live elapsed time for IN GAME rows. Runs when signature is unchanged.
function updateTickFields(s) {
  s.players.forEach(p => {
    const rows = document.querySelectorAll(
      `.player-row[data-rid="${CSS.escape(p.riot_id)}"]`
    );
    rows.forEach(row => {
      const nextEl = row.querySelector('.col-next');
      if (nextEl) {
        const html = renderNextCheck(p);
        if (nextEl.innerHTML !== html) nextEl.innerHTML = html;
      }
      const detailEl = row.querySelector('.col-detail');
      if (detailEl) {
        const txt = tickingDetail(p) || p.status_detail || '';
        if (detailEl.textContent !== txt) detailEl.textContent = txt;
      }
    });
  });
}

function render(s) {
  state = s;

  updateChrome(s);

  // Auto-collapse any expanded rows whose player is no longer in a live
  // game — stops stale rosters from sticking around after the game ends.
  if (expandedRows.size) {
    const keep = new Set();
    s.players.forEach(p => {
      if (expandedRows.has(p.riot_id)
          && LIVE_STATES.has(p.status)
          && Array.isArray(p.live_participants)
          && p.live_participants.length) {
        keep.add(p.riot_id);
      }
    });
    expandedRows = keep;
  }

  // Fast-path: nothing structural changed → just update the ticking
  // text in place. This avoids re-creating DOM nodes every second,
  // which was restarting every CSS animation and making the live
  // dropdown flicker.
  const sig = structuralSignature(s);
  if (sig === _lastSignature) {
    updateTickFields(s);
    return;
  }
  _lastSignature = sig;

  // Full rebuild path ─────────────────────────────────────────────────
  const playerMap = {};
  s.players.forEach(p => playerMap[p.riot_id] = p);

  const inTeam = new Set();
  s.teams.forEach(t => t.members.forEach(m => inTeam.add(m)));

  let html = '';

  // Teams
  s.teams.forEach(team => {
    const cls = STATUS_CLASS[team.status] || 't-safe';
    const dot = STATUS_DOT[team.status] || '○';
    const collapsed = collapsedTeams.has(team.name) ? 'collapsed' : '';
    const selCls = (selectedKind === 'team' && selectedId === team.name) ? 'selected' : '';
    // Mirror the alert-level convention from player rows so the folder box
    // itself glows / breathes according to severity, not just the pill.
    const folderAlert =
        team.status === 'TEAM IN GAME' ? 'folder-alert-critical'
      : team.status === 'TEAM QUEUING' ? 'folder-alert-high'
      : team.status === 'TEAM RECENT'  ? 'folder-alert-mid'
      : team.status === 'TEAM PARTIAL' ? 'folder-alert-low'
      : '';

    html += `
      <div class="team-folder ${collapsed} ${selCls} ${folderAlert}"
           data-team="${esc(team.name)}"
           data-drop-target="team">
        <div class="team-header"
             data-team-toggle="${esc(team.name)}"
             data-selectable="team"
             data-id="${esc(team.name)}">
          <div class="team-name col-name">
            <span class="team-caret">▾</span>
            <span class="team-folder-icon">📁</span>
            <span class="team-name-text">${esc(team.name)}</span>
            <span class="team-count">${team.members.length}</span>
          </div>
          <div class="col-status ${cls}">
            <span class="status-pill">
              <span class="status-dot">${dot}</span>
              <span class="status-label">${team.status}</span>
            </span>
          </div>
          <div class="col-detail">${esc(team.status_detail)}</div>
          <div class="col-mode">${renderModeChip(team.last_mode)}</div>
          <div class="col-next"></div>
        </div>
        <div class="team-members-wrap" data-team-body="${esc(team.name)}">
          ${team.members.map(rid => {
            const p = playerMap[rid];
            return p ? renderPlayerRow(p, team.name) : '';
          }).join('')}
          <div class="team-drop-zone" data-drop-zone="${esc(team.name)}"></div>
        </div>
      </div>`;
  });

  // Ungrouped players — wrapped in an explicit drop target so the
  // whole section (including the label and the empty space after the
  // last row) accepts drops out of teams.
  const ungrouped = s.players.filter(p => !inTeam.has(p.riot_id));
  html += `<div class="ungrouped-wrap" data-drop-target="ungrouped">`;
  if (s.teams.length > 0) {
    html += `<div class="ungrouped-label">Ungrouped</div>`;
  }
  ungrouped.forEach(p => { html += renderPlayerRow(p, null); });
  // Tail spacer gives a clear "drop at end" region when ungrouped is
  // short or empty — without it the section has nothing to target.
  html += `<div class="ungrouped-tail"></div>`;
  html += `</div>`;

  $list.innerHTML = html;

  // Show/hide empty state
  const hasContent = s.players.length > 0;
  $list.style.display = hasContent ? 'flex' : 'none';
  $empty.style.display = hasContent ? 'none' : 'block';

  // Re-bind row events after DOM rebuild
  bindRowEvents();
}

function renderPlayerRow(player, teamName) {
  const disabled = player.enabled === false;
  const cls = STATUS_CLASS[player.status] || 's-loading';
  const dot = STATUS_DOT[player.status] || '·';
  const selCls = (selectedKind === 'player' && selectedId === player.riot_id) ? 'selected' : '';
  // Pile an "alert" modifier on the row itself for high-danger statuses
  // (suppressed entirely for disabled players)
  const alertCls = disabled ? '' :
                  (player.status === 'IN GAME')          ? 'alert-critical'
                  : (player.status === 'QUEUING LIKELY')  ? 'alert-high'
                  : (player.status === 'RECENTLY DONE')   ? 'alert-mid'
                  : (player.status === 'PLAYED THIS HOUR') ? 'alert-low' : '';
  const disabledCls = disabled ? 'row-disabled' : '';

  // Detail text is derived client-side when the status has a ticking
  // timestamp — IN GAME (live elapsed) or post-game "Ended Xm ago". For
  // everything else we fall back to whatever the backend last stored.
  const detailText = tickingDetail(player) || player.status_detail || '';

  const canExpand = !disabled
                    && LIVE_STATES.has(player.status)
                    && Array.isArray(player.live_participants)
                    && player.live_participants.length > 0;
  const isExpanded = canExpand && expandedRows.has(player.riot_id);
  const expandCls  = canExpand ? 'row-expandable' : '';
  const openCls    = isExpanded ? 'row-open' : '';

  const rowHtml = `
    <div class="player-row ${selCls} ${alertCls} ${disabledCls} ${expandCls} ${openCls}"
         data-rid="${esc(player.riot_id)}"
         data-team="${esc(teamName || '')}"
         draggable="true">
      <div class="col-name">
        ${canExpand ? '<span class="row-caret">▸</span>' : ''}
        ${esc(player.riot_id)}
      </div>
      <div class="col-status ${cls}">
        <span class="status-pill">
          <span class="status-dot">${dot}</span>
          <span class="status-label">${player.status}</span>
        </span>
      </div>
      <div class="col-detail">${esc(detailText)}</div>
      <div class="col-mode">${disabled ? '' : renderModeChip(player.last_mode)}</div>
      <div class="col-next">${renderNextCheck(player)}</div>
    </div>`;

  const panelHtml = isExpanded ? renderLivePanel(player) : '';
  return rowHtml + panelHtml;
}

// ── Live-game participants dropdown ────────────────────────────────────────
function renderLivePanel(player) {
  const parts = player.live_participants || [];
  const myTeam = player.my_team_id;
  // Split into ally / enemy. If my_team_id is unknown (rare), fall back
  // to showing everyone under a single "All players" heading.
  let ally = [], enemy = [];
  if (myTeam != null) {
    parts.forEach(p => (p.team_id === myTeam ? ally : enemy).push(p));
  } else {
    ally = parts;
  }

  const renderOne = (p) => {
    const nameBits = [];
    if (p.champion)  nameBits.push(`<span class="lp-champ">${esc(p.champion)}</span>`);
    if (p.riot_id)   nameBits.push(`<span class="lp-rid">${esc(p.riot_id)}</span>`);
    if (p.bot)       nameBits.push(`<span class="lp-bot">BOT</span>`);
    const targetCls = p.is_target ? 'lp-target' : '';
    return `<li class="lp-item ${targetCls}">${nameBits.join('') || '—'}</li>`;
  };

  const mode = player.last_mode ? `<span class="lp-mode">${esc(player.last_mode)}</span>` : '';
  const allyBlock = ally.length ? `
      <div class="lp-group lp-ally">
        <div class="lp-heading">${myTeam != null ? 'Their Team' : 'All Players'}</div>
        <ul class="lp-list">${ally.map(renderOne).join('')}</ul>
      </div>` : '';
  const enemyBlock = enemy.length ? `
      <div class="lp-group lp-enemy">
        <div class="lp-heading">Enemy Team</div>
        <ul class="lp-list">${enemy.map(renderOne).join('')}</ul>
      </div>` : '';

  return `
    <div class="live-panel" data-rid-panel="${esc(player.riot_id)}">
      <div class="lp-header">
        <span class="lp-title">Live game roster</span>
        ${mode}
      </div>
      <div class="lp-body">
        ${allyBlock}${enemyBlock}
      </div>
    </div>`;
}

function renderNextCheck(player) {
  if (player.enabled === false)         return '';
  if (player.next_check_sec == null)    return '';
  if (player.next_check_sec < 0)        return '';   // disabled / never
  const s = player.next_check_sec;
  const label = s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2,'0')}s`;
  // Hottest cadences get a warmer color via a class
  const cad = player.cadence_sec || 0;
  let cls = 'next-cold';
  if (cad <= 20)      cls = 'next-hot';
  else if (cad <= 60) cls = 'next-warm';
  else if (cad <= 150) cls = 'next-mid';
  return `<span class="next-chip ${cls}" title="Cadence: ${cad}s">${label}</span>`;
}

// If the player is IN GAME and we have a game start timestamp, compute
// the live elapsed time on the client so the detail string ticks every
// render (every 1s) without making any API calls.
function liveDetail(player) {
  if (player.status !== 'IN GAME') return null;
  if (player.enabled === false)    return null;
  const start = player.live_game_start_ts;
  if (!start) return null; // champ select / loading — leave backend string
  const elapsed = Math.max(0, Math.floor(Date.now() / 1000 - start));
  const m = Math.floor(elapsed / 60);
  const s = elapsed % 60;
  return `Live · ${m}m ${String(s).padStart(2, '0')}s into game`;
}

// Statuses whose backend `status_detail` starts with "Ended Xm ago …".
// The number goes stale between polls (up to 2.5 min for PLAYED THIS HOUR),
// so OP.GG and this app could disagree by several minutes. We recompute the
// leading phrase client-side from the underlying timestamp each render and
// splice it back into the backend's detail string, preserving the suffix
// (outcome, duration, games-this-hour, or "post-game").
const ENDED_STATUSES = new Set([
  'QUEUING LIKELY', 'RECENTLY DONE', 'PLAYED THIS HOUR',
]);

function freshEndedDetail(player) {
  if (!ENDED_STATUSES.has(player.status)) return null;
  if (player.enabled === false) return null;
  const detail = player.status_detail || '';
  // Parse the backend's baked "Ended Xm ago" / "Ended <1m ago" prefix so
  // we can compare fresh-vs-baked and sanity-check the tick.
  const m = detail.match(/^Ended (<1m|(\d+)m) ago(.*)$/s);
  if (!m) return null;
  const bakedMins = m[1] === '<1m' ? 0 : parseInt(m[2], 10);
  const rest = m[3];

  // Pick the reference timestamp based on which backend branch produced
  // the detail string:
  //   • "… · post-game" → backend's live-sighting grace window. Match-v5
  //     hasn't indexed the just-finished game yet, so last_match_end_ms
  //     refers to some PRIOR completed match — could be hours or days old.
  //     Use live_seen_ts (the last spectator sighting) as the closest
  //     proxy for when the current game ended.
  //   • otherwise → normal completion path, last_match_end_ms is the
  //     actual end timestamp from match-v5.
  const isPostGame = rest.includes('post-game');
  const endSec = isPostGame
    ? (player.live_seen_ts || 0)
    : (player.last_match_end_ms
        ? player.last_match_end_ms / 1000
        : (player.live_seen_ts || 0));
  if (!endSec) return null;

  const freshMins = Math.max(0, Math.floor((Date.now() / 1000 - endSec) / 60));

  // Sanity cap: if freshening would produce a number wildly out of line
  // with what the backend last wrote, the underlying timestamp is stale
  // (polls have been failing / the app was offline / the API key expired)
  // and freshening just amplifies the lie — "Ended 551m ago · 2 games
  // this hour" is a nonsense row. Normal poll cadence tops out around
  // 4 min, so a drift of >15 min means something's wrong with the data
  // pipeline; fall back to the backend's static string in that case.
  // The row will still be stale, but at least it's not confidently
  // wrong by hundreds of minutes.
  if (freshMins - bakedMins > 15) return null;

  const head = freshMins < 1 ? 'Ended <1m ago' : `Ended ${freshMins}m ago`;
  return head + rest;
}

// Single entry point for whichever live string should be rendered for this
// player. Keeps the render + tick paths in lockstep.
function tickingDetail(player) {
  return liveDetail(player) || freshEndedDetail(player);
}

function renderModeChip(mode) {
  if (!mode) return '';
  const key = String(mode).toLowerCase();
  let modCls = 'mode-default';
  if (key.includes('ranked solo'))                    modCls = 'mode-ranked-solo';
  else if (key.includes('ranked flex'))               modCls = 'mode-ranked-flex';
  else if (key.includes('aram'))                      modCls = 'mode-aram';
  else if (key.includes('arena'))                     modCls = 'mode-arena';
  else if (key.includes('urf'))                       modCls = 'mode-urf';
  else if (key.includes('clash'))                     modCls = 'mode-clash';
  else if (key.includes('swiftplay'))                 modCls = 'mode-swiftplay';
  else if (key.includes('quickplay'))                 modCls = 'mode-quickplay';
  else if (key.includes('normal'))                    modCls = 'mode-normal';
  else if (key.includes('co-op') || key.includes('bot')) modCls = 'mode-bot';
  else if (key.includes('custom'))                    modCls = 'mode-custom';
  return `<span class="mode-chip ${modCls}">${esc(mode)}</span>`;
}

function esc(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Row event binding (called after each render) ───────────────────────────────
function bindRowEvents() {
  // Team header toggle + select
  document.querySelectorAll('[data-team-toggle]').forEach(el => {
    el.addEventListener('click', e => {
      if (e.button !== 0) return;
      const name = el.dataset.teamToggle;
      if (collapsedTeams.has(name)) collapsedTeams.delete(name);
      else collapsedTeams.add(name);
      selectItem('team', name);
      render(state);
    });
    el.addEventListener('contextmenu', e => {
      e.preventDefault();
      selectItem('team', el.dataset.id);
      showContextMenu(e, 'team', el.dataset.id, '');
    });
  });

  // Player row select + drag + right-click
  document.querySelectorAll('.player-row').forEach(el => {
    const rid  = el.dataset.rid;
    const team = el.dataset.team || null;

    el.addEventListener('click', e => {
      if (e.button !== 0) return;
      selectItem('player', rid);
      // Toggle live-game roster panel on click if this row is expandable.
      // `row-expandable` is only set for players whose status is in
      // LIVE_STATES and who have non-empty live_participants, so clicking
      // anywhere else just selects without side effects.
      if (el.classList.contains('row-expandable')) {
        if (expandedRows.has(rid)) expandedRows.delete(rid);
        else                        expandedRows.add(rid);
        render(state);
      }
    });

    el.addEventListener('contextmenu', e => {
      e.preventDefault();
      selectItem('player', rid);
      showContextMenu(e, 'player', rid, team);
    });

    // HTML5 drag
    el.addEventListener('dragstart', e => {
      dragRiotId    = rid;
      dragOriginTeam = team;
      el.classList.add('dragging');

      // Build a richer ghost card so the dragged item reads as a
      // "lifted" version of the row, not a tiny text chip. Name +
      // status pill echoes the row's own layout.
      const player = (state && state.players || []).find(p => p.riot_id === rid);
      const statusCls = player ? (STATUS_CLASS[player.status] || 's-safe') : 's-safe';
      const statusDot = player ? (STATUS_DOT[player.status]   || '·')      : '·';
      const statusLbl = player ? player.status : '';
      $dragGhost.innerHTML = `
        <div class="drag-ghost-name">${esc(rid)}</div>
        <span class="status-pill ${statusCls}">
          <span class="status-dot">${statusDot}</span>
          <span class="status-label">${esc(statusLbl)}</span>
        </span>`;
      $dragGhost.classList.add('visible');
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setDragImage(new Image(), 0, 0); // use custom ghost
    });
    el.addEventListener('dragend', () => {
      el.classList.remove('dragging');
      $dragGhost.classList.remove('visible');
      $dragGhost.innerHTML = '';
      document.querySelectorAll(
        '.drop-indicator-above, .drop-indicator-below, ' +
        '.drag-over, .drop-target-active, .drag-over-row'
      ).forEach(x => x.classList.remove(
        'drop-indicator-above', 'drop-indicator-below',
        'drag-over', 'drop-target-active', 'drag-over-row'
      ));
      dragRiotId = null;
    });
  });
}

// ── Free-drag drop handling (installed ONCE at boot) ─────────────────
// CRITICAL: these listeners are document-level and must NOT be attached
// inside bindRowEvents(), which is called on every render (~1×/sec).
// Re-attaching them per render was a listener leak that compounded into
// heavy jitter and progressive lag — every dragover event triggered a
// stack of redundant `elementFromPoint` + DOM-query passes that grew
// without bound while the app was open.
//
// The handlers read module-level `dragRiotId` live, so one install at
// boot is enough to cover all future renders.
//
// Behavior:
// - One dragover handler figures out the insertion point each tick,
//   diffs against last-rendered indicator state to avoid DOM thrash.
// - The indicator is unified: `drop-indicator-above` on the row the
//   item will insert before, with `drop-indicator-below` on the last
//   row as the sole "append at end of section" case.
// - On drop we poll() immediately so the UI reflects the new order
//   without waiting for the next 1 s tick.
let _dragHandlersInstalled = false;
let _dragPendingDrop = null;
let _dragLastIndicator = null;
let _dragLastSectionNode = null;

function _clearDragDropVisuals() {
  document.querySelectorAll(
    '.drop-indicator-above, .drop-indicator-below, ' +
    '.drag-over, .drop-target-active'
  ).forEach(x => x.classList.remove(
    'drop-indicator-above', 'drop-indicator-below',
    'drag-over', 'drop-target-active'
  ));
  _dragLastIndicator = null;
  _dragLastSectionNode = null;
}

function _dragSectionOfEl(el) {
  if (!el) return null;
  const folder = el.closest && el.closest('.team-folder');
  if (folder) return { kind: 'team', name: folder.dataset.team, node: folder };
  const ung = el.closest && el.closest('[data-drop-target="ungrouped"]');
  if (ung) return { kind: 'ungrouped', name: null, node: ung };
  return null;
}

function installDragHandlers() {
  if (_dragHandlersInstalled) return;
  _dragHandlersInstalled = true;

  document.addEventListener('dragover', e => {
    if (!dragRiotId) return;

    // The ghost is pointer-events:none so elementFromPoint sees the
    // actual row / section under the cursor.
    const hit = document.elementFromPoint(e.clientX, e.clientY);
    const section = _dragSectionOfEl(hit);
    if (!section) {
      if (_dragLastIndicator || _dragLastSectionNode) _clearDragDropVisuals();
      _dragPendingDrop = null;
      return;
    }

    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';

    // Rows in this section, minus the one being dragged (so hovering
    // over its own former slot doesn't produce a weird indicator).
    const rows = Array.from(section.node.querySelectorAll('.player-row'))
      .filter(r => r.dataset.rid !== dragRiotId);

    // Find the insertion point: first row whose vertical midpoint is
    // BELOW the cursor → insert before it. If none qualify, we're past
    // the last row → append.
    let targetRow = null;
    for (const r of rows) {
      const rect = r.getBoundingClientRect();
      if (e.clientY < rect.top + rect.height / 2) { targetRow = r; break; }
    }

    let newIndicator;   // { row, side } | null
    let beforeRid;
    if (targetRow) {
      newIndicator = { row: targetRow, side: 'above' };
      beforeRid = targetRow.dataset.rid;
    } else if (rows.length > 0) {
      newIndicator = { row: rows[rows.length - 1], side: 'below' };
      beforeRid = null;
    } else {
      newIndicator = null;           // empty section → section highlight only
      beforeRid = null;
    }

    // Diff against previous indicator. Only touch DOM when it actually
    // moves — that kills the flicker from rewriting classes every tick.
    const sameIndicator = _dragLastIndicator && newIndicator
      && _dragLastIndicator.row === newIndicator.row
      && _dragLastIndicator.side === newIndicator.side;

    if (!sameIndicator) {
      if (_dragLastIndicator) {
        _dragLastIndicator.row.classList.remove(
          'drop-indicator-above', 'drop-indicator-below'
        );
      }
      if (newIndicator) {
        newIndicator.row.classList.add(
          newIndicator.side === 'above'
            ? 'drop-indicator-above'
            : 'drop-indicator-below'
        );
      }
      _dragLastIndicator = newIndicator;
    }

    // Section highlight — only toggle when the section changes.
    if (section.node !== _dragLastSectionNode) {
      if (_dragLastSectionNode) _dragLastSectionNode.classList.remove('drop-target-active');
      section.node.classList.add('drop-target-active');
      _dragLastSectionNode = section.node;
    }

    _dragPendingDrop = {
      targetTeam: section.kind === 'team' ? section.name : null,
      beforeRid,
    };
  });

  document.addEventListener('drop', async e => {
    if (!dragRiotId || !_dragPendingDrop) return;
    e.preventDefault();
    const { targetTeam, beforeRid } = _dragPendingDrop;
    const movedRid = dragRiotId;
    _dragPendingDrop = null;
    dragRiotId = null;
    _clearDragDropVisuals();
    await api('move_player', movedRid, targetTeam || '', beforeRid || '');
    // Don't wait for the next 1 s poll tick to reflect the move —
    // pull state immediately so the row lands where it was dropped.
    poll();
  });

  // Clean up visuals if the drag is abandoned off-list.
  document.addEventListener('dragleave', e => {
    // Only reset if the drag left the window entirely
    if (e.clientX === 0 && e.clientY === 0) _clearDragDropVisuals();
  });

  // Track ghost position. Offset keeps the card tucked under the cursor
  // so it reads as "held" rather than trailing far to one side.
  document.addEventListener('dragover', e => {
    $dragGhost.style.left = (e.clientX - 12) + 'px';
    $dragGhost.style.top  = (e.clientY - 12) + 'px';
  }, { passive: true });
}

function selectItem(kind, id) {
  selectedKind = kind;
  selectedId   = id;
  // Refresh selected state without a full API poll
  document.querySelectorAll('.player-row.selected, .team-folder.selected, .team-header.selected')
    .forEach(el => el.classList.remove('selected'));
  if (kind === 'player') {
    document.querySelectorAll(`[data-rid="${CSS.escape(id)}"]`)
      .forEach(el => el.classList.add('selected'));
  } else {
    document.querySelectorAll(`[data-team="${CSS.escape(id)}"].team-folder`)
      .forEach(el => el.classList.add('selected'));
  }
}

// ── Context menu ──────────────────────────────────────────────────────────────
function showContextMenu(event, kind, id, teamName) {
  hideContextMenu();
  let html = '';

  if (kind === 'team') {
    html += ctxItem('✏', `Rename "${id}"`, `renameTeam('${jsesc(id)}')`);
    html += ctxItem('+', 'Add Player to Team', `addPlayerToTeam('${jsesc(id)}')`);
    html += `<div class="ctx-separator"></div>`;
    html += ctxItem('✕', 'Remove Team', `removeTeam('${jsesc(id)}')`, true);
  } else {
    html += ctxItem('✏', `Rename "${id}"`, `renamePlayer('${jsesc(id)}')`);

    // Move to team submenu
    if (state && state.teams.length > 0) {
      html += `<div class="ctx-submenu-wrap">`;
      html += `<div class="ctx-item">Move to Team</div>`;
      html += `<div class="ctx-submenu">`;
      state.teams.forEach(t => {
        const check = t.name === teamName ? '✓' : '';
        html += `<div class="ctx-item" onclick="movePlayer('${jsesc(id)}','${jsesc(t.name)}')">
                   <span class="ctx-check">${check}</span>${esc(t.name)}
                 </div>`;
      });
      if (teamName) {
        html += `<div class="ctx-separator"></div>`;
        html += `<div class="ctx-item" onclick="movePlayer('${jsesc(id)}',null)">
                   <span class="ctx-check"></span>(Ungrouped)
                 </div>`;
      }
      html += `</div></div>`;
    }

    if (teamName) {
      html += ctxItem('↩', 'Remove from Team', `movePlayer('${jsesc(id)}',null)`);
    }

    // Disable / enable toggle
    const playerObj = state && state.players.find(p => p.riot_id === id);
    const isDisabled = playerObj && playerObj.enabled === false;
    if (isDisabled) {
      html += ctxItem('▶', 'Resume Checks', `setPlayerEnabled('${jsesc(id)}',true)`);
    } else {
      html += ctxItem('⏸', 'Pause Checks', `setPlayerEnabled('${jsesc(id)}',false)`);
    }

    html += `<div class="ctx-separator"></div>`;
    html += ctxItem('✕', 'Stop Tracking', `stopTracking('${jsesc(id)}')`, true);
  }

  $ctxMenu.innerHTML = html;
  $ctxMenu.classList.add('visible');

  // Position
  const vw = window.innerWidth, vh = window.innerHeight;
  let x = event.clientX, y = event.clientY;
  $ctxMenu.style.left = '0'; $ctxMenu.style.top = '0';
  const w = $ctxMenu.offsetWidth, h = $ctxMenu.offsetHeight;
  if (x + w > vw) x = vw - w - 6;
  if (y + h > vh) y = vh - h - 6;
  $ctxMenu.style.left = x + 'px';
  $ctxMenu.style.top  = y + 'px';
}

function ctxItem(icon, label, onclick, danger = false) {
  const cls = danger ? 'ctx-item danger' : 'ctx-item';
  return `<div class="${cls}" onclick="${onclick}">
            <span style="width:16px;text-align:center;font-size:11px">${icon}</span>
            ${esc(label)}
          </div>`;
}

function jsesc(s) {
  return String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function hideContextMenu() {
  $ctxMenu.classList.remove('visible');
  $ctxMenu.innerHTML = '';
}

document.addEventListener('click', e => {
  if (!$ctxMenu.contains(e.target)) hideContextMenu();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { hideContextMenu(); closeAllModals(); }
});

// ── Context menu actions (called from inline onclick) ──────────────────────────
window.renameTeam = (name) => { hideContextMenu(); openRenameModal('team', name); };
window.addPlayerToTeam = (name) => { hideContextMenu(); openAddPlayerModal(name); };
window.removeTeam = async (name) => {
  hideContextMenu();
  const ok = await confirmModal({
    title:       'Remove team',
    message:     `Remove team "${name}"?`,
    hint:        'Members stay tracked as ungrouped players.',
    confirmText: 'Remove',
    variant:     'danger',
  });
  if (!ok) return;
  await api('remove_team', name);
};
window.renamePlayer = (rid) => { hideContextMenu(); openRenameModal('player', rid); };
window.movePlayer = async (rid, teamName) => {
  hideContextMenu();
  await api('move_player', rid, teamName);
};
window.stopTracking = async (rid) => {
  hideContextMenu();
  const ok = await confirmModal({
    title:       'Stop tracking',
    message:     `Stop tracking ${rid}?`,
    hint:        'Removes them from the list. You can re-add them any time.',
    confirmText: 'Stop tracking',
    variant:     'danger',
  });
  if (!ok) return;
  await api('remove_player', rid);
};
window.setPlayerEnabled = async (rid, enabled) => {
  hideContextMenu();
  await api('set_player_enabled', rid, enabled);
};

// ── Button bindings ───────────────────────────────────────────────────────────
function bindButtons() {
  document.getElementById('btn-settings').addEventListener('click', openSettingsModal);
  document.getElementById('btn-add-player').addEventListener('click', () => {
    const team = selectedKind === 'team' ? selectedId : null;
    openAddPlayerModal(team);
  });
  document.getElementById('btn-add-team').addEventListener('click', () => {
    openModal('modal-add-team');
    document.getElementById('input-team-name').value = '';
    document.getElementById('input-team-name').focus();
  });
  document.getElementById('btn-refresh').addEventListener('click', async () => {
    await api('refresh_now');
  });
  document.getElementById('btn-remove').addEventListener('click', () => {
    if (!selectedId) { showToast('Select a player or team first', true); return; }
    if (selectedKind === 'team') window.removeTeam(selectedId);
    else window.stopTracking(selectedId);
  });

  // Settings save
  document.getElementById('btn-save-settings').addEventListener('click', async () => {
    const key  = document.getElementById('input-api-key').value.trim();
    const plat = document.getElementById('input-platform').value;
    if (!key) { showToast('API key is required', true); return; }
    const r = await api('set_config', key, plat);
    if (r && !r.ok) { showToast(r.error, true); return; }
    closeModal('modal-settings');
    showToast('Settings saved');
  });

  // Add player confirm
  document.getElementById('btn-confirm-add-player').addEventListener('click', async () => {
    const rid  = document.getElementById('input-riot-id').value.trim();
    const team = document.getElementById('input-add-team').value || null;
    if (!rid) { showToast('Enter a Riot ID', true); return; }
    const r = await api('add_player', rid, team);
    if (r && !r.ok) { showToast(r.error, true); return; }
    closeModal('modal-add-player');
  });
  document.getElementById('input-riot-id').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('btn-confirm-add-player').click();
  });

  // Add team confirm
  document.getElementById('btn-confirm-add-team').addEventListener('click', async () => {
    const name = document.getElementById('input-team-name').value.trim();
    if (!name) { showToast('Enter a team name', true); return; }
    const r = await api('add_team', name);
    if (r && !r.ok) { showToast(r.error, true); return; }
    closeModal('modal-add-team');
  });
  document.getElementById('input-team-name').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('btn-confirm-add-team').click();
  });

  // Rename confirm
  document.getElementById('btn-confirm-rename').addEventListener('click', doRename);
  document.getElementById('input-rename').addEventListener('keydown', e => {
    if (e.key === 'Enter') doRename();
  });
}

// ── Modal helpers ─────────────────────────────────────────────────────────────
function bindModalClose() {
  document.querySelectorAll('[data-close]').forEach(btn => {
    btn.addEventListener('click', () => closeModal(btn.dataset.close));
  });
  document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', e => {
      if (e.target === overlay) closeModal(overlay.id);
    });
  });
}

function openModal(id) {
  document.getElementById(id).classList.add('open');
}
function closeModal(id) {
  document.getElementById(id).classList.remove('open');
}
function closeAllModals() {
  document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'));
}

// Styled replacement for window.confirm(). Resolves true on confirm,
// false on cancel / Escape / overlay click. Reuses #modal-confirm.
function confirmModal({title='Confirm', message='', hint='',
                        confirmText='Confirm', cancelText='Cancel',
                        variant='danger'} = {}) {
  return new Promise(resolve => {
    const $title  = document.getElementById('confirm-title');
    const $msg    = document.getElementById('confirm-message');
    const $hint   = document.getElementById('confirm-hint');
    const $ok     = document.getElementById('btn-confirm-ok');
    const $cancel = document.getElementById('btn-confirm-cancel');

    $title.textContent = title;
    $msg.textContent   = message;
    if (hint) { $hint.textContent = hint; $hint.style.display = ''; }
    else      { $hint.textContent = '';   $hint.style.display = 'none'; }

    $ok.textContent    = confirmText;
    $cancel.textContent = cancelText;
    $ok.className = 'btn ' + (variant === 'danger' ? 'btn-danger' : 'btn-primary');

    openModal('modal-confirm');
    // Focus the cancel button by default so Enter doesn't auto-confirm
    // a destructive action when the modal opens.
    setTimeout(() => $cancel.focus(), 0);

    const cleanup = (result) => {
      $ok.removeEventListener('click', onOk);
      $cancel.removeEventListener('click', onCancel);
      document.removeEventListener('keydown', onKey);
      document.getElementById('modal-confirm')
              .removeEventListener('click', onOverlay);
      closeModal('modal-confirm');
      resolve(result);
    };
    const onOk     = () => cleanup(true);
    const onCancel = () => cleanup(false);
    const onKey    = (e) => {
      if (e.key === 'Escape') cleanup(false);
      else if (e.key === 'Enter' && document.activeElement === $ok) cleanup(true);
    };
    const onOverlay = (e) => {
      if (e.target.id === 'modal-confirm') cleanup(false);
    };

    $ok.addEventListener('click', onOk);
    $cancel.addEventListener('click', onCancel);
    document.addEventListener('keydown', onKey);
    document.getElementById('modal-confirm').addEventListener('click', onOverlay);
  });
}

function openSettingsModal() {
  if (state) {
    document.getElementById('input-api-key').value = state.api_key_set ? '••••••••' : '';
    document.getElementById('input-platform').value = state.platform || 'NA1';
  }
  openModal('modal-settings');
  document.getElementById('input-api-key').focus();
}

function openAddPlayerModal(teamName) {
  const titleEl = document.getElementById('add-player-title');
  titleEl.textContent = teamName ? `Add to "${teamName}"` : 'Add Player';
  document.getElementById('input-add-team').value = teamName || '';
  document.getElementById('input-riot-id').value = '';
  openModal('modal-add-player');
  document.getElementById('input-riot-id').focus();
}

// Rename modal state
let renameKind = null;
let renameOldValue = null;

function openRenameModal(kind, current) {
  renameKind = kind;
  renameOldValue = current;
  document.getElementById('rename-title').textContent = kind === 'team' ? 'Rename Team' : 'Update Riot ID';
  document.getElementById('rename-label').textContent = kind === 'team' ? 'New team name' : 'New Riot ID';
  document.getElementById('rename-hint').style.display = kind === 'player' ? 'block' : 'none';
  document.getElementById('input-rename').value = current;
  openModal('modal-rename');
  const inp = document.getElementById('input-rename');
  inp.focus();
  inp.select();
}

async function doRename() {
  const val = document.getElementById('input-rename').value.trim();
  if (!val || val === renameOldValue) { closeModal('modal-rename'); return; }
  let r;
  if (renameKind === 'team') r = await api('rename_team', renameOldValue, val);
  else                       r = await api('rename_player', renameOldValue, val);
  if (r && !r.ok) { showToast(r.error, true); return; }
  closeModal('modal-rename');
}

// ── Platform select population ─────────────────────────────────────────────────
function populatePlatformSelect() {
  const sel = document.getElementById('input-platform');
  sel.innerHTML = platforms.map(p => `<option value="${p}">${p}</option>`).join('');
}

// ── Toast ─────────────────────────────────────────────────────────────────────
let toastTimer = null;
function showToast(msg, isError = false) {
  $toast.textContent = msg;
  $toast.className = 'toast visible' + (isError ? ' error' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $toast.classList.remove('visible'); }, 3000);
}

// ── Settings auto-open if not configured ──────────────────────────────────────
window.addEventListener('pywebviewready', () => {
  // Triggered once pywebview is fully initialized
  setTimeout(async () => {
    const s = await getState();
    if (!s.configured) openSettingsModal();
  }, 300);
});
