// Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
// All rights reserved.

import { beforeAll, describe, expect, it } from "vitest";

// Importing the module registers the custom elements (side effect at load).
beforeAll(async () => {
  await import("../../custom_components/vigil/frontend/vigil-card.js");
});

/** A one-issue payload with a single app issue populated. */
function sampleData(overrides = {}) {
  const app = {
    kind: "app_failed",
    name: "Rclone Backup",
    integration: "App",
    detail: "crashed (error state)",
    since: "2026-07-11T13:57:00+00:00",
    duration_seconds: 3600,
    since_is_lower_bound: false,
    source: "rclone",
    device_id: null,
    entity_id: null,
    config_entry_id: null,
    domain: null,
  };
  return {
    issues: [app],
    integration_failures: [],
    devices_offline: [],
    stale_devices: [],
    device_faults: [],
    app_issues: [app],
    counts: {
      total: 1,
      integration_failures: 0,
      devices_offline: 0,
      stale_devices: 0,
      device_faults: 0,
      app_issues: 1,
    },
    integration_health: [],
    last_run: "2026-07-11T14:57:00+00:00",
    healthy: false,
    startup_grace_active: false,
    ...overrides,
  };
}

/** Render a card element with injected data and return its shadow-root HTML. */
async function renderCard(type, data) {
  const el = document.createElement(type);
  el.setConfig({});
  el.hass = { states: {}, callApi: async () => data };
  // Let the async _refresh() (callApi -> render) settle.
  await new Promise((resolve) => setTimeout(resolve, 0));
  return el.shadowRoot.innerHTML;
}

describe("dashboard cards render the app issues bucket", () => {
  // The full and issues cards list the issues (name shows); the summary card
  // shows only counts (the tile label shows).
  for (const type of ["vigil-card", "vigil-issues-card"]) {
    it(`${type} lists app issues linked to the app page`, async () => {
      const html = await renderCard(type, sampleData());
      expect(html).toContain("App Issues");
      expect(html).toContain("Rclone Backup");
      // The name links to the app info page under Settings (slug from
      // source): /config/app/<slug>/info.
      expect(html).toContain('href="/config/app/rclone/info"');
    });
  }

  it("vigil-summary-card shows the app count tile", async () => {
    const html = await renderCard("vigil-summary-card", sampleData());
    expect(html).toContain("App issues");
  });

  it("the app table uses an App column, drops Integration, shows For", async () => {
    const el = document.createElement("vigil-issues-card");
    el.setConfig({ section: "apps" });
    el.hass = { states: {}, callApi: async () => sampleData() };
    await new Promise((resolve) => setTimeout(resolve, 0));
    const html = el.shadowRoot.innerHTML;
    expect(html).toContain("Rclone Backup");
    // Correct headers for apps: "App" + "For", no "Device"/"Integration".
    // (The formatted duration itself is browser-only via Intl.DurationFormat,
    // which jsdom lacks, so we assert the column, not the rendered value.)
    expect(html).toContain("<th>App</th>");
    expect(html).toContain("<th>For</th>");
    expect(html).not.toContain("<th>Device</th>");
    expect(html).not.toContain("<th>Integration</th>");
  });
});
