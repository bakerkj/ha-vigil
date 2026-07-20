// Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
// All rights reserved.

// The card references its types as globals (it's a plain served module with no
// imports). This file bridges two sources into that global scope:
//
//   1. The API data model — VigilData / VigilIssue / IntegrationHealthRow — is
//      GENERATED from the backend wire types into ./vigil-api.generated.d.ts
//      (see scripts/gen_frontend_types.py). We only alias them into global here.
//   2. The rest are frontend-only shapes with no backend origin (the counts
//      dict, the card config, the card's own section/column view-models, and
//      the minimal Home Assistant surface the card touches) — declared here.

import type {
  VigilData as GeneratedVigilData,
  VigilIssue as GeneratedVigilIssue,
  IntegrationHealthRow as GeneratedIntegrationHealthRow,
} from "./vigil-api.generated";

export {};

declare global {
  // --- generated API model, re-exported into global scope --------------------
  type VigilData = GeneratedVigilData;
  type VigilIssue = GeneratedVigilIssue;
  type IntegrationHealthRow = GeneratedIntegrationHealthRow;

  // --- frontend-only shapes (no backend origin) ------------------------------

  /** Per-bucket issue counts (VigilData.counts): ``total`` plus one entry per
   * ISSUE_BUCKETS key. A plain string→number map — the keys are assembled from
   * ISSUE_BUCKETS at runtime, so this matches the generated ``counts`` type and
   * still allows access by a computed key. */
  interface VigilCounts {
    [key: string]: number;
  }

  /** One column of a grouped issue table. */
  interface IssueColumns {
    nameHeader?: string;
    showIntegration?: boolean;
  }

  /** A grouped issue section (ISSUE_SECTIONS entry). Its ``key`` always names
   * one of the VigilData buckets that hold a ``VigilIssue[]``. */
  interface IssueSection {
    title: string;
    key:
      | "integration_failures"
      | "devices_offline"
      | "stale_devices"
      | "device_faults"
      | "app_issues";
    severity: "bad" | "warn";
    columns?: IssueColumns;
  }

  /** The Lovelace card config (only ``section`` is read by the issues card). */
  interface VigilCardConfig {
    section?: string;
  }

  /** A single HA state-machine entry (only the fields the card reads). */
  interface HassState {
    state: string;
    last_updated: string;
    attributes: Record<string, unknown>;
  }

  /** The minimal Home Assistant object the card depends on. */
  interface HomeAssistant {
    states: Record<string, HassState | undefined>;
    callApi(method: string, path: string): Promise<VigilData>;
  }
}
