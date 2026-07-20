# Vigil — Home Assistant health watchdog

Vigil watches your Home Assistant install for the failures that normally go
unnoticed for days: an integration that crashed, a device that dropped offline,
or a sensor that is still "available" but quietly stopped sending data. It
surfaces them through diagnostic sensors you can wire into your own automations
and a dashboard card for an at-a-glance health view, plus an **optional** single
persistent notification (off by default) that groups every current issue and
updates in place.

Vigil is designed for large installs (hundreds of devices) and deliberately
favours a **low false-positive rate over fast detection** — it would rather tell
you about a real problem a few minutes late than cry wolf.

## What it detects

Vigil runs three built-in detection engines every cycle (plus optional custom
watch rules and, on Supervised installs, app health — see
[Custom watch rules](#custom-watch-rules) and [App health](#app-health)):

1. **Integration failures** — config entries not in the `loaded` state
   (`setup_error`, `setup_retry`, `not_loaded`, `migration_error`).
2. **Devices offline** — every entity on a device unavailable beyond a grace
   period. Battery / sleepy devices get an extended grace. Distinguishes
   _network-confirmed_ offline from _no-signal_ offline.
3. **Silent devices** — device is reachable (network UP) but its data has gone
   stale: no update in `multiplier × expected_interval`, where the expected
   interval is **learned** from each entity's own history (with per-device-class
   heuristics as a fallback).
4. **Device faults** _(optional)_ — a value you designate on a specific
   integration's sensor isn't "ok" (e.g. an ESPHome `*_component_*` status
   sensor reading `warning`/`error`). Rules are **your** config, not shipped
   with Vigil — see [Custom watch rules](#custom-watch-rules).
5. **App health** _(optional, Supervised installs only)_ — a Supervisor app that
   has crashed, failed to start despite `boot: auto`, or is restart-looping. On
   by default, and no-ops entirely on non-Supervised installs; see
   [App health](#app-health).

### How "offline" is decided (connectivity resolution)

For each device, Vigil resolves an UP / DOWN / UNKNOWN connectivity state from
the best available evidence, in priority order — all of it native to the Home
Assistant registries, with **no configuration**:

1. A same-device `connectivity` binary_sensor (e.g. a ping/`device_pulse` or
   ESPHome API-status sensor).
2. A protocol-native status entity (Z-Wave JS `node_status`; sleeping nodes are
   treated as UNKNOWN, not offline).
3. MAC-address correlation to a router / AP / switch / scanner `device_tracker`
   (UniFi, Aruba, Omada, TP-Link, Mikrotik, Netgear, Fing, …).
4. Otherwise UNKNOWN — Vigil uses conservative thresholds and makes no strong
   claims.

> **Not yet covered:** a dedicated MQTT availability-topic / Last-Will signal
> and router ARP/DHCP tables are not resolved directly. MQTT devices are still
> caught indirectly — when they drop, their entities go `unavailable`, which
> Engine 2 detects — but a Tasmota/Z2M Last-Will "offline" is not yet read as a
> first-class DOWN signal. Planned.

## Output

The always-on surfaces are the diagnostic sensors and the dashboard card; the
persistent notification is opt-in (see the note below).

- **Diagnostic sensors** you can automate on (WhatsApp, Signal, etc.):
  `sensor.vigil_total_issues`, `sensor.vigil_integration_failures`,
  `sensor.vigil_devices_offline`, `sensor.vigil_stale_devices`,
  `sensor.vigil_device_faults`, `sensor.vigil_app_issues` (each a plain count),
  plus `sensor.vigil_state` (overall status `ok` / `issues` / `starting`, with
  the counts as attributes). The full per-issue detail is served by the card
  feed at `/api/vigil/state` — it is deliberately kept off the sensor attributes
  to stay within the recorder's 16 KB attribute limit.
- **A dashboard card** (`custom:vigil-card`, auto-registered as a Lovelace
  resource) you add to any dashboard — a color-coded per-integration health
  table with device drill-down. (This is a Lovelace card, not a sidebar panel.)
- **An optional persistent notification** (`notification_id: vigil_issues`),
  **off by default** — enable **Create persistent notification** in the options.
  It groups all current issues into one in-place notification, deleted
  automatically when everything is healthy, and dismissing it acknowledges the
  shown issues until they clear and return.

  > **Note:** the notification is off by default because it's a newer surface
  > that hasn't yet had the real-world mileage the sensors and card have. It's
  > fully implemented and tested — enable it if you want an at-a-glance alert
  > without building a card — but the sensors/card are the recommended primary
  > surfaces for now.

## Installation

**Requirements:** Home Assistant **2026.6.0** or newer (running on Python
**3.14.2+**). Vigil uses Python 3.14 syntax and will not load on older cores.

### HACS (recommended)

1. In HACS → Integrations → ⋮ → **Custom repositories**, add
   `https://github.com/bakerkj/ha-vigil` as an **Integration**.
2. Install **Vigil**, then restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Vigil**.

### Manual

Copy `custom_components/vigil` into your Home Assistant
`config/custom_components` directory and restart, then add the integration from
the UI.

## Configuration

Everything is configured from the UI (initial setup and **Configure** /
options). Vigil is single-instance.

| Option                                               | Default   | Description                                                                                                                                             |
| ---------------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Scan interval (seconds)                              | 60        | How often the detection cycle runs.                                                                                                                     |
| Grace period (minutes)                               | 15        | How long a device must be unavailable before alert.                                                                                                     |
| Staleness multiplier                                 | 3         | `N` in the `N × expected_interval` staleness rule.                                                                                                      |
| Startup ignore (seconds)                             | 300       | Suppress all alerts during Home Assistant startup.                                                                                                      |
| Battery device grace multiplier                      | 2         | Extra grace for battery / UNKNOWN-connectivity devices.                                                                                                 |
| Create persistent notification                       | off       | Opt in to a single in-place notification grouping all issues (see [Output](#output)). Off by default — the sensors / card are the primary surfaces.     |
| Monitor app health                                   | on        | Watch Supervisor apps for crashes / restart loops. No-op on non-Supervised installs. See [App health](#app-health).                                     |
| Recorder lookback (days)                             | 0 (auto)  | Recorder history used to reconstruct outage start; `0` auto-matches recorder retention.                                                                 |
| Excluded domains / entities / devices / integrations | —         | Skip these entirely (all engines).                                                                                                                      |
| Excluded apps                                        | —         | Supervisor app slugs to skip in app-health monitoring.                                                                                                  |
| Exclude integrations / devices from staleness        | —         | Skip only the silent/stale check for these; still detect them going offline.                                                                            |
| Annotation platforms ignored for availability        | —         | Platforms whose entities stay "available" when the device is offline, so they can't mask an outage.                                                     |
| Interval store URL (external DB)                     | — (local) | Optional SQLAlchemy URL (e.g. a separate MariaDB) for the learned intervals; blank uses the local SQLite file. Use a **separate** DB from the recorder. |

> **Tip — annotation platforms ignored for availability.** A device is judged
> offline only when **all** its entities are unavailable, so a single entity
> that stays "available" on a dead device masks the outage. Add such platforms
> here. The usual culprits are **computed-from-history helpers** that report a
> value regardless of any device's state — they can only ever hide an outage,
> never prove a device is reachable: `history_stats`, `statistics`, `trend`,
> `min_max`, `derivative`, `integration`, `threshold`. (Only relevant if you've
> attached one to a real device.) Annotation integrations like `battery_notes`
> belong here for the same reason. This does **not** hide the entity elsewhere —
> it only stops it voting on whether its device is offline.

### Custom watch rules

Beyond the built-in engines, a deployment-specific **`vigil.yaml`** in your Home
Assistant config directory (alongside `configuration.yaml`) adds two optional
sections — `watch:` and `ignore:`. It lives in **your own config**, never in the
integration; no file = both features off, and edits are picked up automatically
on the next cycle. (The legacy `vigil_watch.yaml` — a bare list of watch rules —
is still read when `vigil.yaml` is absent.)

**Watch rules** (`watch:`) flag a device when one of its entities holds a value
that isn't "ok" — for example an ESPHome `component_warning` / `component_error`
sensor.

Each rule must name an **integration** (the entity's platform) **and** at least
one **entity** criterion — both must match, so a same-named sensor from another
integration is never caught by accident. A matched entity whose state isn't in
`ok_states` produces a **Device fault** issue attributed to that entity's
device, which then flows through the notification, card,
`sensor.vigil_device_faults`, and acknowledge/clear machinery like any other
issue.

```yaml
# <config>/vigil.yaml  — see vigil.example.yaml
watch:
  - name: ESPHome component health
    integration: esphome # REQUIRED — the entity's platform must equal this
    match: # REQUIRED — at least one; all listed must match
      entity_id_glob: "sensor.*_component_*" # fnmatch: * ? [seq]
      # entity_id_suffix: "_component_error"
      # device_class: problem
      # translation_key: component_status
    ok_states: ["ok", "none", ""] # healthy states (case-insensitive)
    ignore_unavailable: true # skip unavailable/unknown (don't double-flag offline)
    grace_seconds: 0 # must be not-ok this long before flagging (trigger debounce)
    clear_seconds: 0 # once flagged, stay flagged until ok this long (clear hysteresis)
    detail: "Component fault: {state}" # {state} / {entity_id} / {name} / {detail_state}

# Ignore rules suppress a signal for the entities a selector matches (the same
# `integration` + `match` fields as a watch rule). `action: connectivity` stops a
# MISLABELED device_class=connectivity sensor from being read as the device's own
# reachability — e.g. a Litter-Robot's "hopper_connected" accessory sensor. One
# rule covers every such device.
ignore:
  - action: connectivity
    integration: litterrobot
    match:
      device_class: connectivity
      entity_id_suffix: "_hopper_connected"
```

| Rule key               | Required | Default           | Meaning                                                                                               |
| ---------------------- | -------- | ----------------- | ----------------------------------------------------------------------------------------------------- |
| `name`                 | yes      | —                 | Rule label; shown as the issue source.                                                                |
| `integration`          | yes      | —                 | The entity's platform must equal this (e.g. `esphome`).                                               |
| `match`                | yes      | —                 | Entity criteria (below); at least one, all listed must match.                                         |
| `ok_states`            | no       | `["ok"]`          | Healthy states; anything else flags the device.                                                       |
| `ignore_unavailable`   | no       | `true`            | Skip `unavailable`/`unknown` (that's an offline concern).                                             |
| `case_sensitive`       | no       | `false`           | Case-sensitive `ok_states` comparison.                                                                |
| `grace_seconds`        | no       | `0`               | Trigger debounce: must be continuously not-ok this long before flagging.                              |
| `clear_seconds`        | no       | `0`               | Clear hysteresis: once flagged, stay flagged until ok continuously this long.                         |
| `detail_entity_suffix` | no       | —                 | Pull `{detail_state}` from a sibling entity on the same device by id suffix.                          |
| `detail_entity_glob`   | no       | —                 | Same, matching the sibling by `fnmatch` glob.                                                         |
| `detail`               | no       | `Not OK: {state}` | Issue text; `{state}`, `{entity_id}`, `{name}`, `{detail_state}`, `{detail_entity_id}` are filled in. |

`match` accepts `entity_id_glob`, `entity_id_suffix`, `device_class`, and
`translation_key`. A malformed file is logged and ignored (the previous rules
stay in effect), so a mid-edit typo never breaks detection.

## App health

On **Supervised** installs, Vigil also watches your Supervisor apps via the
Supervisor API — no configuration beyond the **Monitor app health** toggle (on
by default). It flags two conditions:

- **App failed** — the app is in the `error` state, or is `stopped` while set to
  `boot: auto` (it should be running and isn't).
- **App unstable** — the app crashed (dropped from a running state to `error`) 3
  or more times within 30 minutes (a restart loop). This supersedes a plain
  "failed" while it persists.

A manually stopped app (`boot: manual`) is treated as intentional and never
flagged. A one-shot app (`startup: once` — it runs and exits by design, e.g. the
HAOS Configurator) is likewise not flagged just for being stopped, though an
`error`-state one-shot still is. App issues flow through the same notification,
card, and `sensor.vigil_app_issues` count as every other issue, and per-app
flap/streak state is persisted so a restart loop is still visible across an HA
restart. On non-Supervised installs the engine no-ops entirely.

## How it works

```
config entries ─┐
device registry ─┼─▶ connectivity resolution ─▶ per-device tuples ─┐
entity states ──┘                                                   │
                              ┌──── Engine 1 (integrations)─────────┤
                              ├──── Engine 2 (offline)──────────────┤
                              ├──── Engine 3 (stale)────────────────┤
                              ├──── Engine 4 (watch rules)──────────┼─▶ suppression ─▶ notification
Supervisor apps ─────────────└──── Engine 5 (app health)───────────┘                 + sensors + card
```

Learned update intervals are persisted in `.storage/vigil_intervals.db` (SQLite,
unless an external interval-store URL is configured) and survive restarts.
Engine 3 only starts judging an entity after it has watched it for a couple of
days (a full daily cycle), so it learns the entity's normal longest gap — e.g.
an overnight quiet period — before it will call the device stale.

For how the **code** is laid out — the package structure, the dependency tiers,
and how a cycle flows through the coordinator/pipeline — see
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Development

```bash
uv sync                                    # create the venv
uv run python -m pytest tests/             # tests
uv run python -m pytest tests/ --cov=custom_components/vigil  # tests + coverage
uv run mypy custom_components/vigil tests   # strict type-checking
uv run ruff check custom_components tests   # lint (same ruff the hook pins)
uvx prek run --all-files                    # lint / format / all hooks
npm run gen:types                           # regenerate the frontend API types
```

The dashboard card's API types (`frontend/vigil-api.generated.d.ts`) are
**generated** from the backend wire types (`models.VigilStateDict`) via Pydantic
JSON Schema → `json-schema-to-typescript`; `models.py` is the single source of
truth. Don't edit that file by hand — run `npm run gen:types` after changing the
serialized shape. A prek hook and the `gen-types` CI job fail on drift.

Install the git hooks (conventional commits + pre-commit):

```bash
uvx prek install --overwrite --hook-type pre-commit --hook-type commit-msg
```

## License

[MIT](LICENSE) © 2026 Kenneth Baker.
