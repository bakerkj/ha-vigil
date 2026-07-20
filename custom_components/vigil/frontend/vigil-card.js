/*
 * Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
 * All rights reserved.
 */

// Safety-net poll only. The card primarily refreshes event-driven — when Vigil's
// own state sensor advances (once per detection cycle) — so this interval just
// covers the rare case where the state entity was renamed or an update was missed.
const REFRESH_INTERVAL_MS = 30000;

/**
 * Escape user-derived text before inserting into innerHTML.
 * @param {*} value
 * @returns {string}
 */
function esc(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** HA frontend route for a device's page.
 * @param {string} deviceId
 * @returns {string}
 */
function deviceUrl(deviceId) {
  return `/config/devices/device/${encodeURIComponent(deviceId)}`;
}

/** HA frontend route for an integration's page.
 * @param {string} domain
 * @returns {string}
 */
function integrationUrl(domain) {
  return `/config/integrations/integration/${encodeURIComponent(domain)}`;
}

/** Route for an app's info page (slug carried in issue.source). Apps now
 * live under the Settings panel at ``/config/app/<slug>/info`` — a /config
 * sub-route like devices/integrations, so in-app navigation resolves it.
 * @param {string} slug
 * @returns {string}
 */
function appUrl(slug) {
  return `/config/app/${encodeURIComponent(slug)}/info`;
}

/** Issue kinds produced by Engine 5 (app health); their name links to the
 * app info page. */
const APP_KINDS = new Set(["app_failed", "app_unstable"]);

/** An escaped internal link (text and href both escaped).
 *
 * Keeps a real href (accessibility, right-click, fallback) but is marked so the
 * card's delegated handler navigates via the HA frontend's client-side router
 * instead of a full page load — a plain anchor would make the companion app
 * hand off to an external browser.
 * @param {string} href
 * @param {string} text
 * @returns {string}
 */
function linkHtml(href, text) {
  return `<a class="nav" href="${esc(href)}">${esc(text)}</a>`;
}

/** A numeric table cell, flagged with a severity class only when non-zero.
 * @param {number} value
 * @param {string} severity  the class added when value > 0 (e.g. "bad", "warn")
 * @returns {string}
 */
function numCell(value, severity) {
  const cls = value > 0 ? `num ${severity}` : "num";
  return `<td class="${cls}">${esc(value)}</td>`;
}

/** Navigate within the HA single-page app (works inside the companion app).
 * @param {string} path
 */
function navigateTo(path) {
  history.pushState(null, "", path);
  window.dispatchEvent(
    new CustomEvent("location-changed", { bubbles: true, composed: true }),
  );
}

// Compact, locale-aware duration formatting via the browser-native
// Intl.DurationFormat ("narrow" style -> "2h 5m", "1d 3h"). We only choose which
// two units to show; Intl renders the numbers and localized unit labels. Guarded
// so a browser without Intl.DurationFormat degrades to no duration rather than
// throwing at module load.
const DURATION_FORMAT =
  typeof Intl.DurationFormat === "function"
    ? new Intl.DurationFormat(undefined, { style: "narrow" })
    : null;

/**
 * Format a duration in seconds as a compact string (e.g. "2h 5m").
 * @param {number | null | undefined} seconds
 * @returns {string}
 */
function formatDuration(seconds) {
  const total = Math.floor(Number(seconds));
  if (!Number.isFinite(total) || total <= 0 || !DURATION_FORMAT) {
    return "";
  }
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  let parts;
  if (days > 0) {
    parts = { days, hours };
  } else if (hours > 0) {
    parts = { hours, minutes };
  } else if (minutes > 0) {
    parts = { minutes };
  } else {
    parts = { seconds: secs };
  }
  return DURATION_FORMAT.format(parts);
}

/**
 * Format an ISO-8601 timestamp into a localized, human-readable string.
 * @param {string} iso
 * @returns {string}
 */
function formatTimestamp(iso) {
  if (!iso) {
    return "never";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return String(iso);
  }
  try {
    return date.toLocaleString();
  } catch (err) {
    return date.toString();
  }
}

// --- Shared render helpers ---------------------------------------------------
//
// Free functions (not bound to a card instance) so every card type renders the
// header pill, integration-health table, and issue tables identically.

/** The header: title, status pill, and last-run timestamp.
 * @param {VigilData} data
 * @returns {string}
 */
function headerHtml(data) {
  const total = Number((data.counts || {}).total || 0);
  const healthy = data.healthy && total === 0;
  let pill;
  if (data.startup_grace_active) {
    pill = `<span class="pill paused"><span class="dot"></span>Starting…</span>`;
  } else if (healthy) {
    pill = `<span class="pill ok"><span class="dot"></span>All clear</span>`;
  } else {
    pill = `<span class="pill bad"><span class="dot"></span>${esc(total)} issue${total === 1 ? "" : "s"}</span>`;
  }
  const lastRun = `<span class="last-run">Last run: ${esc(formatTimestamp(data.last_run))}</span>`;
  return `
    <div class="header">
      <h1>Vigil</h1>
      ${pill}
      ${lastRun}
    </div>
  `;
}

/** Lovelace card-size heuristic: base rows plus one per ~2 items, capped at 20.
 * @param {number} base
 * @param {number | undefined} n
 * @returns {number}
 */
function sizedRows(base, n) {
  return Math.min(20, base + Math.ceil(Number(n || 0) / 2));
}

/** The six count tiles, in display order: [label, counts key, severity class].
 * @type {Array<[string, keyof VigilCounts, string]>}
 */
const COUNT_TILES = [
  ["Total", "total", "bad"],
  ["Integration failures", "integration_failures", "bad"],
  ["Devices offline", "devices_offline", "bad"],
  ["Silent / stale", "stale_devices", "warn"],
  ["Device faults", "device_faults", "bad"],
  ["App issues", "app_issues", "bad"],
];

/** Six labeled count tiles from ``data.counts``.
 * @param {VigilCounts} counts
 * @returns {string}
 */
function countTilesHtml(counts) {
  const tiles = COUNT_TILES.map(([label, key, cls]) => {
    const value = Number(counts[key] || 0);
    return `
    <div class="tile ${value > 0 ? cls : ""}">
      <div class="tile-num">${esc(value)}</div>
      <div class="tile-label">${esc(label)}</div>
    </div>
  `;
  }).join("");
  return `<div class="tiles">${tiles}</div>`;
}

/** The startup-grace "Paused on startup" notice (issues are suppressed). */
function pausedNoticeHtml() {
  return `
    <div class="card all-clear paused">
      <div class="check">⏸</div>
      <div class="msg">Paused on startup — detection resumes shortly</div>
    </div>
  `;
}

/** A compact "all clear" tick card.
 * @param {string} msg
 * @returns {string}
 */
function allClearHtml(msg) {
  return `
    <div class="card all-clear">
      <div class="check">✓</div>
      <div class="msg">${esc(msg)}</div>
    </div>
  `;
}

/** Per-integration health table with a "Show N healthy" toggle.
 *
 * ``showHealthy`` controls whether the (green, uninteresting) healthy rows are
 * shown; the returned toggle carries id ``toggle-healthy`` so the owning card
 * can wire its click handler after injecting the HTML.
 * @param {IntegrationHealthRow[]} integrations
 * @param {boolean} showHealthy
 * @returns {string}
 */
function integrationTableHtml(integrations, showHealthy) {
  if (!Array.isArray(integrations) || integrations.length === 0) {
    return `<div class="card"><h2>Integration Health</h2><div class="muted">No integrations reported.</div></div>`;
  }

  /**
   * @param {IntegrationHealthRow} it
   * @returns {string}
   */
  const rowHtml = (it) => {
    const offline = Number(it.offline_count || 0);
    const stale = Number(it.stale_count || 0);
    const faults = Number(it.fault_count || 0);
    const devices = Number(it.device_count || 0);
    let rowClass = "row-ok";
    if (it.failed || offline > 0 || faults > 0) {
      rowClass = "row-bad";
    } else if (stale > 0) {
      rowClass = "row-warn";
    }
    const title = it.title || it.domain || "Unknown";
    const titleCell = it.domain
      ? linkHtml(integrationUrl(it.domain), title)
      : esc(title);
    return `
      <tr class="${rowClass}">
        <td>${titleCell}</td>
        <td>${esc(it.state)}</td>
        <td class="num">${esc(devices)}</td>
        ${numCell(offline, "bad")}
        ${numCell(stale, "warn")}
        ${numCell(faults, "bad")}
      </tr>
    `;
  };

  // Collapse the "healthy" (green, uninteresting) integrations by default;
  // a toggle reveals them when wanted.
  const healthy = integrations.filter((it) => it.healthy);
  const interesting = integrations.filter((it) => !it.healthy);
  const shown = showHealthy ? integrations : interesting;
  const rows = shown.map(rowHtml).join("");

  const toggle = healthy.length
    ? `<span id="toggle-healthy" class="toggle">${
        showHealthy ? "Hide healthy" : `Show ${healthy.length} healthy`
      }</span>`
    : "";

  const body = rows
    ? `<table>
        <thead>
          <tr>
            <th>Integration</th>
            <th>State</th>
            <th>Devices</th>
            <th>Offline</th>
            <th>Stale</th>
            <th>Faults</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`
    : `<div class="muted">All ${healthy.length} integrations healthy.</div>`;

  return `
    <div class="card">
      <div class="card-head">
        <h2>Integration Health</h2>
        ${toggle}
      </div>
      ${body}
    </div>
  `;
}

/** Default severity for an issue whose section did not pin one.
 * @param {string} kind
 * @returns {"bad" | "warn"}
 */
function severityForKind(kind) {
  if (kind === "silent_device") {
    return "warn";
  }
  return "bad";
}

/** One issue row (name link, optional integration, "for" duration, detail).
 * ``showIntegration`` drops the integration column (e.g. for apps).
 * @param {VigilIssue} issue
 * @param {"bad" | "warn"} severity
 * @param {boolean} showIntegration
 * @returns {string}
 */
function issueRowHtml(issue, severity, showIntegration) {
  const rowClass = severity === "warn" ? "row-warn" : "row-bad";
  // Compact duration via the native Intl.DurationFormat. Prefer the backend's
  // duration_seconds (as of last_run); fall back to a live value from `since`.
  let secs = issue.duration_seconds;
  if ((secs === null || secs === undefined) && issue.since) {
    secs = (Date.now() - new Date(issue.since).getTime()) / 1000;
  }
  let since = formatDuration(secs);
  // "≥" when the start is only a lower bound (down longer than the lookback).
  if (since && issue.since_is_lower_bound) {
    since = `≥ ${since}`;
  }
  // Device name links to its device page; integration-level issues link to the
  // integration page; app issues link to the Supervisor app info page.
  const nameText = issue.name || "Unknown";
  let nameHtml;
  if (issue.device_id) {
    nameHtml = linkHtml(deviceUrl(issue.device_id), nameText);
  } else if (issue.domain) {
    nameHtml = linkHtml(integrationUrl(issue.domain), nameText);
  } else if (APP_KINDS.has(issue.kind) && issue.source) {
    nameHtml = linkHtml(appUrl(issue.source), nameText);
  } else {
    nameHtml = esc(nameText);
  }
  const cells = [`<td>${nameHtml}</td>`];
  if (showIntegration) {
    const integrationHtml = issue.domain
      ? linkHtml(
          integrationUrl(issue.domain),
          issue.integration || issue.domain,
        )
      : esc(issue.integration || "");
    cells.push(`<td class="muted">${integrationHtml}</td>`);
  }
  cells.push(`<td class="num">${esc(since)}</td>`);
  cells.push(`<td class="muted">${esc(issue.detail || "")}</td>`);
  return `<tr class="${rowClass}">${cells.join("")}</tr>`;
}

// One compact table, one row per issue — far tighter than per-issue cards when
// dozens of devices are offline. `severity` null => derive per row. `columns`
// customizes the first header and whether the integration column shows (apps
// have no device/integration, so they use "App" and drop it).
/**
 * @param {string} title
 * @param {VigilIssue[]} issues
 * @param {"bad" | "warn" | null} severity
 * @param {IssueColumns} [columns]
 * @returns {string}
 */
function issueTableHtml(title, issues, severity, columns) {
  const nameHeader = (columns && columns.nameHeader) || "Device";
  const showIntegration = !columns || columns.showIntegration !== false;
  const rows = issues
    .map((/** @type {VigilIssue} */ issue) =>
      issueRowHtml(
        issue,
        severity || severityForKind(issue.kind),
        showIntegration,
      ),
    )
    .join("");
  const headers = [`<th>${esc(nameHeader)}</th>`];
  if (showIntegration) {
    headers.push(`<th>Integration</th>`);
  }
  headers.push(`<th>For</th>`, `<th>Detail</th>`);
  return `
    <div class="card">
      <h2>${esc(title)} (${issues.length})</h2>
      <table>
        <thead>
          <tr>${headers.join("")}</tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

// The grouped issue sections, keyed for the issues-card `section` config.
/** @type {Record<string, IssueSection>} */
const ISSUE_SECTIONS = {
  failures: {
    title: "Integration Failures",
    key: "integration_failures",
    severity: "bad",
  },
  offline: {
    title: "Devices Offline",
    key: "devices_offline",
    severity: "bad",
  },
  stale: {
    title: "Silent / Stale Devices",
    key: "stale_devices",
    severity: "warn",
  },
  faults: {
    title: "Device Faults",
    key: "device_faults",
    severity: "bad",
  },
  apps: {
    title: "App Issues",
    key: "app_issues",
    severity: "bad",
    // Apps aren't devices/integrations: label the name column and drop the
    // integration column.
    columns: { nameHeader: "App", showIntegration: false },
  },
};

/** Render one or more issue sections.
 *
 * ``which`` is one of "all" | "failures" | "offline" | "stale" | "faults" |
 * "apps". Only non-empty sections render; if nothing matches, a compact "all
 * clear" message is shown.
 * @param {VigilData} data
 * @param {string} which
 * @returns {string}
 */
function issueSectionsHtml(data, which) {
  const order =
    which === "all"
      ? ["failures", "offline", "stale", "faults", "apps"]
      : [which];

  let html = "";
  for (const name of order) {
    const section = ISSUE_SECTIONS[name];
    const issues = Array.isArray(data[section.key]) ? data[section.key] : [];
    if (issues.length === 0) {
      continue;
    }
    html += issueTableHtml(
      section.title,
      issues,
      section.severity,
      section.columns,
    );
  }

  if (html) {
    return html;
  }

  // Nothing in the requested scope. For "all", fall back to the flat issue
  // list (counts can be non-zero with grouped lists empty), else "all clear".
  if (which === "all") {
    const issues = Array.isArray(data.issues) ? data.issues : [];
    if (issues.length) {
      return issueTableHtml("Issues", issues, null);
    }
    return allClearHtml("All clear — no issues detected");
  }
  return allClearHtml(`No ${ISSUE_SECTIONS[which].title.toLowerCase()}`);
}

// --- Shared base card --------------------------------------------------------

/**
 * Base for every Vigil card. Owns the hass setter (which refetches
 * ``/api/vigil/state`` when Vigil's state sensor advances — event-driven, not a
 * fixed poll — with a slow safety-net interval), the shadow-root shell, the
 * shared styles, and the delegated ``a.nav`` SPA-router click handler.
 *
 * Subclasses override ``_renderBody(data)`` to return the inner HTML for their
 * section(s); the base wraps it in ``<ha-card>`` and adds the standard
 * loading / no-data / error-banner states. Subclasses may override
 * ``_afterRender()`` to wire up any interactive bits (e.g. the health toggle).
 */
class VigilBaseCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    /** @type {VigilCardConfig} */
    this._config = {};
    /** @type {HomeAssistant | null} */
    this._hass = null;
    /** @type {VigilData | null} */
    this._data = null;
    /** @type {boolean} */
    this._error = false;
    /** @type {boolean} */
    this._loaded = false;
    /** @type {boolean} */
    this._fetching = false;
    /** @type {ReturnType<typeof setInterval> | null} */
    this._interval = null;
    /** @type {boolean} */
    this._rendered = false;
    // Last-seen Vigil cycle stamp; a change means new data to fetch.
    /** @type {string | null} */
    this._stamp = null;
    // Whether the integration-health table shows healthy (green) rows; toggled by
    // the shared "Show N healthy" control. Ignored by cards without that table.
    /** @type {boolean} */
    this._showHealthy = false;
  }

  // --- Lovelace card contract (subclasses may override) --------------------

  /** Store the card config. Tolerate an empty/absent config.
   * @param {VigilCardConfig} config
   */
  setConfig(config) {
    this._config = config || {};
  }

  getCardSize() {
    return 4;
  }

  /** @param {HomeAssistant | null} hass */
  set hass(hass) {
    this._hass = hass;
    // Render once so the shell + styles exist.
    if (!this._rendered) {
      this._render();
    }
    // Refetch the feed only when Vigil actually ran a new cycle — keyed on its
    // state sensor's ``last_run`` (falling back to last_updated) — not on every
    // unrelated HA state change. Event-driven: near-instant on a real update,
    // zero idle polling in between. (The interval is only a safety net.)
    const st = hass && hass.states ? hass.states["sensor.vigil_state"] : null;
    const stamp = st
      ? /** @type {string | undefined} */ (
          st.attributes && st.attributes.last_run
        ) || st.last_updated
      : null;
    if (!this._loaded || stamp !== this._stamp) {
      this._stamp = stamp;
      this._refresh();
    }
  }

  /** @returns {HomeAssistant | null} */
  get hass() {
    return this._hass;
  }

  // --- Lifecycle ------------------------------------------------------------

  connectedCallback() {
    if (!this._rendered) {
      this._render();
    }
    if (!this._interval) {
      this._interval = setInterval(() => this._refresh(), REFRESH_INTERVAL_MS);
    }
  }

  disconnectedCallback() {
    if (this._interval) {
      clearInterval(this._interval);
      this._interval = null;
    }
  }

  async _refresh() {
    if (!this._hass || this._fetching) {
      return;
    }
    this._fetching = true;
    try {
      const data = await this._hass.callApi("GET", "vigil/state");
      this._data = data;
      this._error = false;
      this._loaded = true;
    } catch (err) {
      // Keep last good data if we have it; just flag the error.
      this._error = true;
      this._loaded = true;
    } finally {
      this._fetching = false;
      this._renderContent();
    }
  }

  _render() {
    this._rendered = true;
    if (!this.shadowRoot) {
      return;
    }
    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <ha-card>
        <div class="vigil-root">
          <div id="content"></div>
        </div>
      </ha-card>
    `;
    // Delegated handler (attached once; survives content re-renders): route
    // internal links through the HA SPA router so navigation stays in-app.
    this.shadowRoot.addEventListener("click", (/** @type {Event} */ ev) => {
      const me = /** @type {MouseEvent} */ (ev);
      // Let the browser handle modified/non-primary clicks (open-in-new-tab,
      // etc.) — only hijack a plain left-click for in-app SPA navigation.
      if (
        me.button !== 0 ||
        me.metaKey ||
        me.ctrlKey ||
        me.shiftKey ||
        me.altKey
      ) {
        return;
      }
      const target = /** @type {Element | null} */ (me.target);
      const anchor = target && target.closest ? target.closest("a.nav") : null;
      if (!anchor) {
        return;
      }
      const href = anchor.getAttribute("href");
      if (!href || !href.startsWith("/")) {
        return;
      }
      me.preventDefault();
      navigateTo(href);
    });
    this._renderContent();
  }

  _renderContent() {
    if (!this.shadowRoot) {
      return;
    }
    const content = this.shadowRoot.getElementById("content");
    if (!content) {
      return;
    }

    if (!this._loaded && !this._data) {
      content.innerHTML = `<div class="loading">Loading Vigil…</div>`;
      return;
    }

    const banner = (/** @type {string} */ msg) =>
      `<div class="card"><div class="muted">${msg}</div></div>`;

    if (!this._data) {
      // No data ever loaded and we hit an error.
      content.innerHTML = banner("Unable to load Vigil state");
      return;
    }

    let html = "";
    if (this._error) {
      html += banner("Unable to load Vigil state — showing last known data.");
    }
    html += this._renderBody(this._data);
    content.innerHTML = html;

    this._afterRender();
  }

  /** Subclasses return their inner section HTML here.
   * @param {VigilData} _data
   * @returns {string}
   */
  _renderBody(_data) {
    return "";
  }

  /** Wire the shared "Show N healthy" toggle after each content render. The
   * toggle exists only on cards that render the integration-health table; this
   * is a no-op on cards without it. */
  _afterRender() {
    if (!this.shadowRoot) {
      return;
    }
    const toggle = this.shadowRoot.getElementById("toggle-healthy");
    if (toggle) {
      toggle.addEventListener("click", () => {
        this._showHealthy = !this._showHealthy;
        this._renderContent();
      });
    }
  }

  _styles() {
    return `
      :host {
        display: block;
        color: var(--primary-text-color, #212121);
      }
      ha-card {
        overflow: hidden;
      }
      .vigil-root {
        padding: 16px;
        box-sizing: border-box;
        font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
      }
      .header {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 12px;
        margin-bottom: 16px;
      }
      .header h1 {
        font-size: 24px;
        font-weight: 500;
        margin: 0;
        flex: 0 0 auto;
      }
      .pill {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px 12px;
        border-radius: 999px;
        font-size: 13px;
        font-weight: 600;
        color: #fff;
      }
      .pill.ok {
        background: var(--success-color, #4caf50);
      }
      .pill.bad {
        background: var(--error-color, #f44336);
      }
      .pill.paused {
        background: var(--warning-color, #ff9800);
      }
      .last-run {
        color: var(--secondary-text-color, #727272);
        font-size: 13px;
        margin-left: auto;
      }
      .tiles {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
        gap: 12px;
      }
      .tile {
        background: var(--ha-card-background, var(--card-background-color, #fff));
        border-radius: 12px;
        box-shadow: var(--ha-card-box-shadow, 0 2px 2px rgba(0,0,0,0.08));
        padding: 12px 14px;
        box-sizing: border-box;
        border-left: 4px solid var(--divider-color, #e0e0e0);
      }
      .tile.bad { border-left-color: var(--error-color, #f44336); }
      .tile.warn { border-left-color: var(--warning-color, #ff9800); }
      .tile-num {
        font-size: 28px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        line-height: 1.1;
      }
      .tile.bad .tile-num { color: var(--error-color, #f44336); }
      .tile.warn .tile-num { color: var(--warning-color, #ff9800); }
      .tile-label {
        margin-top: 4px;
        font-size: 12px;
        color: var(--secondary-text-color, #727272);
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .card {
        background: var(--ha-card-background, var(--card-background-color, #fff));
        border-radius: 12px;
        box-shadow: var(--ha-card-box-shadow, 0 2px 2px rgba(0,0,0,0.08));
        /* Tight vertical padding (6px) so the section header hugs its table;
           horizontal stays at 14px. margin-bottom is the gap BETWEEN sections. */
        padding: 6px 14px;
        margin-bottom: 4px;
        box-sizing: border-box;
      }
      .card:last-child {
        margin-bottom: 0;
      }
      .card h2 {
        font-size: 16px;
        font-weight: 500;
        margin: 0 0 6px;
      }
      .card-head {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 6px;
      }
      .card-head h2 {
        margin: 0;
      }
      .toggle {
        cursor: pointer;
        user-select: none;
        font-size: 13px;
        color: var(--primary-color, #03a9f4);
        white-space: nowrap;
      }
      .toggle:hover {
        text-decoration: underline;
      }
      table {
        width: 100%;
        border-collapse: collapse;
        font-size: 14px;
      }
      th, td {
        text-align: left;
        /* Vertical padding kept very tight (2px) so dense offline/health tables
           don't feel airy; horizontal stays at 10px for column separation. */
        padding: 2px 10px;
        border-bottom: 1px solid var(--divider-color, #e0e0e0);
      }
      th {
        color: var(--secondary-text-color, #727272);
        font-weight: 500;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      tbody tr {
        border-left: 4px solid transparent;
      }
      tbody tr.row-bad td:first-child {
        box-shadow: inset 4px 0 0 var(--error-color, #f44336);
      }
      tbody tr.row-warn td:first-child {
        box-shadow: inset 4px 0 0 var(--warning-color, #ff9800);
      }
      tbody tr.row-ok td:first-child {
        box-shadow: inset 4px 0 0 var(--success-color, #4caf50);
      }
      td:first-child, th:first-child {
        padding-left: 14px;
      }
      .num {
        font-variant-numeric: tabular-nums;
      }
      .num.bad { color: var(--error-color, #f44336); font-weight: 600; }
      .num.warn { color: var(--warning-color, #ff9800); font-weight: 600; }
      .all-clear {
        text-align: center;
        padding: 48px 16px;
        color: var(--secondary-text-color, #727272);
      }
      .all-clear .check {
        font-size: 48px;
        color: var(--success-color, #4caf50);
        line-height: 1;
      }
      .all-clear .msg {
        margin-top: 12px;
        font-size: 16px;
      }
      .muted {
        color: var(--secondary-text-color, #727272);
        font-size: 14px;
      }
      .loading {
        color: var(--secondary-text-color, #727272);
        padding: 24px;
        text-align: center;
      }
      .dot {
        display: inline-block;
        width: 9px;
        height: 9px;
        border-radius: 50%;
        background: #fff;
      }
      a { color: var(--primary-color, #03a9f4); }
    `;
  }
}

// --- Concrete card types -----------------------------------------------------

/** Header pill + the six counts as labeled tiles. */
class VigilSummaryCard extends VigilBaseCard {
  getCardSize() {
    return 3;
  }

  /** @param {VigilData} data @returns {string} */
  _renderBody(data) {
    const counts = data.counts || {};
    let html = headerHtml(data);
    if (data.startup_grace_active) {
      html += pausedNoticeHtml();
    } else {
      html += `<div class="card">${countTilesHtml(counts)}</div>`;
    }
    return html;
  }
}

/** Per-integration health table with the "Show N healthy" toggle. */
class VigilIntegrationHealthCard extends VigilBaseCard {
  getCardSize() {
    const integrations = (this._data && this._data.integration_health) || [];
    return sizedRows(3, integrations.length);
  }

  /** @param {VigilData} data @returns {string} */
  _renderBody(data) {
    // During startup grace, issues are suppressed so the table would show every
    // integration as healthy — show the paused notice instead ("unknown yet").
    if (data.startup_grace_active) {
      return pausedNoticeHtml();
    }
    return integrationTableHtml(
      data.integration_health || [],
      this._showHealthy,
    );
  }
}

const VALID_ISSUE_SECTIONS = new Set([
  "all",
  "failures",
  "offline",
  "stale",
  "faults",
  "apps",
]);

/** Issue tables; ``section`` config selects all or one grouped section. */
class VigilIssuesCard extends VigilBaseCard {
  constructor() {
    super();
    this._section = "all";
  }

  /** @param {VigilCardConfig} config */
  setConfig(config) {
    const cfg = config || {};
    const section = cfg.section === undefined ? "all" : cfg.section;
    if (!VALID_ISSUE_SECTIONS.has(section)) {
      throw new Error(
        `vigil-issues-card: invalid "section" "${section}"; expected one of ${[
          ...VALID_ISSUE_SECTIONS,
        ].join(", ")}`,
      );
    }
    this._config = cfg;
    this._section = section;
  }

  getCardSize() {
    const data = this._data;
    if (!data) {
      return 4;
    }
    const counts = data.counts || {};
    // Size on the rows this card actually renders: its one section, not the
    // global total (a "faults"-only card shouldn't reserve height for offline
    // devices it never shows).
    const section = ISSUE_SECTIONS[this._section];
    const count = Number(
      section ? counts[section.key] || 0 : counts.total || 0,
    );
    return sizedRows(2, count);
  }

  /** @param {VigilData} data @returns {string} */
  _renderBody(data) {
    // During startup grace, issues are suppressed — show the paused notice.
    if (data.startup_grace_active) {
      return pausedNoticeHtml();
    }
    return issueSectionsHtml(data, this._section);
  }
}

/** The all-in-one card: summary + integration health + issues. */
class VigilCard extends VigilBaseCard {
  getCardSize() {
    const total = Number(((this._data || {}).counts || {}).total || 0);
    return sizedRows(8, total);
  }

  /** @param {VigilData} data @returns {string} */
  _renderBody(data) {
    const counts = data.counts || {};
    const total = Number(counts.total || 0);
    const healthy = data.healthy && total === 0;

    let html = headerHtml(data);

    if (data.startup_grace_active) {
      // Detection is paused during startup: issues are suppressed, so the
      // integration-health table would misleadingly show EVERY integration as
      // healthy. Show only the paused notice until detection resumes.
      html += pausedNoticeHtml();
    } else {
      html += integrationTableHtml(
        data.integration_health || [],
        this._showHealthy,
      );
      if (healthy) {
        html += allClearHtml("All clear — no issues detected");
      } else {
        html += issueSectionsHtml(data, "all");
      }
    }
    return html;
  }
}

// --- Registration ------------------------------------------------------------

window.customCards = window.customCards || [];
const customCards = window.customCards;

/** Define a custom element (guarded) and advertise it to the card picker.
 * @param {string} type
 * @param {typeof HTMLElement} elementClass
 * @param {string} name
 * @param {string} description
 */
function registerCard(type, elementClass, name, description) {
  if (!customElements.get(type)) {
    customElements.define(type, elementClass);
  }
  // Guard the picker entry too, so loading the module twice (e.g. via both a
  // Lovelace resource and a legacy extra-module URL) doesn't duplicate it.
  if (!customCards.some((c) => c && c.type === type)) {
    customCards.push({ type, name, description, preview: false });
  }
}

registerCard(
  "vigil-summary-card",
  VigilSummaryCard,
  "Vigil Summary",
  "Vigil status and issue counts",
);
registerCard(
  "vigil-integration-health-card",
  VigilIntegrationHealthCard,
  "Vigil Integration Health",
  "Per-integration health table",
);
registerCard(
  "vigil-issues-card",
  VigilIssuesCard,
  "Vigil Issues",
  "Vigil issue tables (all or one section)",
);
registerCard(
  "vigil-card",
  VigilCard,
  "Vigil",
  "Vigil health overview (summary + integrations + issues)",
);
