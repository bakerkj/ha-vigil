/* eslint-disable */
// Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
// All rights reserved.
//
// GENERATED from the backend wire types (models.VigilStateDict) by
// scripts/gen_frontend_types.py. Do NOT edit by hand -- run `npm run gen:types`.

/**
 * The JSON/wire shape of VigilData, as served at /api/vigil/state.
 *
 * Root type the frontend generator introspects: the same buckets as VigilData
 * but with issues as VigilIssueDict and last_run as an ISO-8601 string.
 */
export interface VigilData {
  issues: VigilIssue[];
  integration_failures: VigilIssue[];
  devices_offline: VigilIssue[];
  stale_devices: VigilIssue[];
  device_faults: VigilIssue[];
  app_issues: VigilIssue[];
  counts: {
    [k: string]: number;
  };
  integration_health: IntegrationHealthRow[];
  last_run: string;
  healthy: boolean;
  startup_grace_active: boolean;
}
/**
 * The JSON/wire shape of a VigilIssue, as served at /api/vigil/state.
 *
 * Source of truth for the generated frontend VigilIssue type; kept in lockstep
 * with VigilIssue.as_dict (mypy enforces it).
 */
export interface VigilIssue {
  kind: string;
  name: string;
  integration: string;
  detail: string;
  since: string | null;
  duration_seconds: number | null;
  since_is_lower_bound: boolean;
  source: string;
  device_id: string | null;
  entity_id: string | null;
  config_entry_id: string | null;
  domain: string | null;
}
/**
 * One row of the per-integration health table shown in the card.
 */
export interface IntegrationHealthRow {
  domain: string;
  title: string;
  state: string;
  healthy: boolean;
  device_count: number;
  offline_count: number;
  stale_count: number;
  fault_count: number;
  failed: boolean;
}
