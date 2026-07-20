# Vigil — architecture

Vigil is a Home Assistant custom integration that watches your instance for
unhealthy integrations, devices, and apps and surfaces them through diagnostic
sensors and a dashboard card, plus an optional (off by default) in-place
persistent notification. A `DataUpdateCoordinator` runs a periodic **detection
cycle**: it snapshots the device/entity registries, resolves each device's
connectivity, runs five detection engines, suppresses false positives, and
assembles a single `VigilData` payload that every surface reads. It optimizes
for a low false-positive rate over detection speed, so most engines are
grace-gated or learned rather than instantaneous.

This document is the single glossary for the three numbering schemes that
otherwise overlap in the source: **dependency tiers** (0–4), **detection
engines** (1–5), and **layers** (1–5). They count different things — read
[Layers vs. engines vs. tiers](#layers-vs-engines-vs-tiers) first if "Engine 4"
and "Layer 4" appearing a few lines apart has ever confused you. For _what_
Vigil detects and how to configure it see the [README](README.md).

## Dependency tiers

Dependency tiers are a **static import rule**, not a runtime flow: every module
maps to a tier and may import only modules in a tier no higher than its own, so
dependencies point strictly downward toward the leaves. This is **enforced** by
`tests/test_architecture.py`, which parses every module's imports and fails CI
on an upward edge — the layering can't quietly rot as features are added.

| Tier | Modules                                                                                       | Role                                                |
| ---- | --------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| 0    | `models`, `const`, `storage`, `selectors`                                                     | leaves (`models` → `const`, `selectors` → `models`) |
| 1    | `persistence/*`, `history/*`, `detection/inputs`, `learning/*`                                | IO + input assembly                                 |
| 2    | `detection/engines/*`, `detection/suppression`, `reporting/*`                                 | detection + suppression + rollup                    |
| 3    | `pipeline`, `context`                                                                         | composition                                         |
| 4    | `coordinator`, `http_api`, `sensor`/`button`/`entity`/`diagnostics`/`config_flow`, `__init__` | HA wiring (top)                                     |

The guard classifies **every** module (an unclassified new module fails the test
rather than silently inheriting tier-4 freedom) and checks each import per
module, so an intra-`detection` edge like `detection/inputs` (tier 1) →
`detection/suppression` (tier 2) is caught. That is why the exclusion predicates
`is_device_excluded` / `config_entry_is_reportable` live in the `models` leaf:
tier-1 `detection/inputs` applies them without importing upward. Three same-tier
rules the ordering can't express are asserted separately: subsystems never
import the `coordinator`, `reporting` never imports `detection` (its
`ExclusionConfig` lives in `models`), and the leaves take no internal imports.

## Detection engines (1–5)

Each engine is a pure-ish function over the `CycleContext` that returns a list
of `VigilIssue`. Engines never import one another; cross-engine de-duplication
(e.g. a device under a failed integration must not also be reported offline)
happens afterward in suppression. All of them live under `detection/engines/` —
i.e. they are the internals of **Layer 3**, and the "Engine N" number is
independent of the "Layer N" number.

| Engine | Module                   | Detects (issue kinds)                                                                             | Cross-cycle state persisted                                                                             |
| ------ | ------------------------ | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| 1      | `engine1_config_entry`   | config entries stuck failed/unloaded (`INTEGRATION_FAILURE`) — no grace                           | none (reads live config-entry state each cycle)                                                         |
| 2      | `engine2_unavailability` | devices unavailable past a grace period (`DEVICE_OFFLINE_CONFIRMED` / `DEVICE_OFFLINE_NO_SIGNAL`) | per-device `DowntimeRecord` map, `DowntimeRepo` (`ctx.downtime`); read + GC'd in place, recorder-seeded |
| 3      | `engine3_staleness`      | reachable devices whose entities stopped updating within their learned cadence (`SILENT_DEVICE`)  | learned per-entity intervals, `IntervalLearner` (`ctx.learner`) → `persistence/` backend                |
| 4      | `engine4_watch_rules`    | entities off their deployment-authored "ok" values (`DEVICE_FAULT`)                               | per-rule trigger/clear debounce, `FaultState` map, `RuleFaultRepo` (`ctx.fault_state`)                  |
| 5      | `engine5_apps`           | Supervisor apps failed or restart-looping (`APP_FAILED` / `APP_UNSTABLE`)                         | per-app last-state + flap timestamps, `AppHealthRecord` map, `AppHealthRepo` (`ctx.app_health`)         |

Notes: Engine 2 reconstructs the _true_ outage start from the recorder so a
device already down before an HA restart isn't handed a fresh grace, and battery
devices get an extended grace. Engine 3's cadence is bootstrapped from recorder
history once per session. Engine 4 loads rules from `<config>/vigil.yaml` (the
`watch:` section; never shipped) and a load/evaluate failure is isolated so it
can't take detection down; a device going offline _freezes_ an existing fault
rather than clearing it. Engine 5 no-ops entirely on non-Supervised installs.
The four stateful engines persist through the shared interval-store backend (a
local SQLite file or the configured external DB —
`custom_components/vigil/storage.py` `StoreRepo`), not separate HA `.storage`
files.

## Layers

"Layer N" is the **conceptual pipeline stage** vocabulary from the design brief:
a datum flows top to bottom through five horizontal stages, source to output.
Unlike tiers (a static import rule) and engines (parallel detectors), layers
describe the runtime data flow. The numbers appear as docstring/comment markers
in the modules that implement each stage:

| Layer | Stage                                               | Where it lives                                                                                |
| ----- | --------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| 1     | Data sources                                        | HA device/entity registries + `hass.states` — raw input, no Vigil module; consumed by Layer 2 |
| 2     | Connectivity resolution + per-device tuple assembly | `detection/inputs.py` (heuristic tables in `const.py`) → a `DeviceTuple` per device           |
| 3     | Detection engines                                   | `detection/engines/*` — **Engines 1–5 all live here** (see the table above)                   |
| 4     | False-positive suppression                          | `detection/suppression.py` (`suppress_issues`: startup grace + exclusion filters)             |
| 5     | Output / reporting                                  | `reporting/*` (`notification`, `acknowledgement`, `health`, `serialize`) + the HA surfaces    |

### Layers vs. engines vs. tiers

Three schemes, three different things — sharing a number is coincidence, not
correspondence:

- **Layers (1–5)** are the five sequential **pipeline stages** (source → tuples
  → detect → suppress → output). "Layer 4" is the _suppression stage_.
- **Engines (1–5)** are the five **parallel detectors**, and they _all_ sit
  inside **Layer 3**. "Engine 4" is the _watch-rules detector_ — unrelated to
  "Layer 4". The two numberings never map onto each other.
- **Tiers (0–4)** are a **static import constraint** (downward-only), enforced
  by a test. A tier is about who may import whom, not about runtime order:
  Layer-2 `detection/inputs` is a tier-1 module, while the Layer-3 engines are
  tier 2.

So when you see "Engine 4" and "Layer 4" close together: the first is the
watch-rule detector inside the detect stage; the second is the suppression stage
that runs after all engines.

## Connectivity priority ladder

Layer 2 (`detection/inputs.py`, `_resolve_connectivity`) resolves each device's
`connectivity_state` (UP / DOWN / UNKNOWN) by trying signal sources in priority
order and stopping at the first that yields a value. The brief defines eight
paths; the current code implements P1, P2, and P4–P6 (collapsed into one MAC
scan) and falls through to P8 — P3 and P7 are reserved:

| Priority | Source                                                                                              | Status                                                |
| -------- | --------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| P1       | Same-device `device_class: connectivity` binary_sensor (zero-config, e.g. ESPHome API)              | implemented (`connectivity_binary_sensor`)            |
| P2       | Protocol-native status entity on the same device (`zwave_js` `node_status`: alive/dead/asleep)      | implemented (`zwave_node_status`; `asleep` → UNKNOWN) |
| P3       | MQTT availability topic on the same device (Last Will)                                              | reserved                                              |
| P4       | MAC match → router/WiFi `device_tracker` (UniFi, Aruba, Omada, TP-Link, Mikrotik, Netgear, Fing, …) | implemented (`mac:<platform>`)                        |
| P5       | MAC match → `switch_port_pro` wired-port entity                                                     | implemented (folded into the MAC scan)                |
| P6       | Fing scanner entity (MAC-based active scan)                                                         | implemented (folded into the MAC scan)                |
| P7       | ARP/DHCP from a router integration (weakest — cache lingers)                                        | reserved                                              |
| P8       | No signal found → `UNKNOWN` (`source = "none"`), conservative thresholds                            | implemented (fallthrough)                             |

A same-device UP signal from P1/P2 can veto an all-`unknown` telemetry set
(proof of reachability); a shared-MAC router tracker (P4–P6) is deliberately too
weak to veto, so it can't mask a silent-telemetry outage.

## A detection cycle

The `DataUpdateCoordinator` fires every `scan_interval`; `_async_update_data` is
a thin orchestrator that owns **time and IO** while the pipeline owns
**composition**:

```
coordinator._async_update_data():
  1. build per-device tuples        detection/inputs.build_device_tuples        (Layer 2)
  2. seed true downtime             detection/engines/engine2 async_seed_downtime → history/recorder
  3. snapshot Supervisor apps       detection/engines/engine5 async_app_snapshot (Engine 5 input)
  4. assemble the input snapshot    context.CycleContext  (now, options, tuples,
                                     + live handles: learner, downtime, fault_state, app_health, config_store)
  5. run the pipeline               pipeline.run_detection(ctx):
        Engine 1  config-entry      detection/engines/engine1_config_entry
        Engine 2  device offline    detection/engines/engine2_unavailability
        Engine 3  silent / stale    detection/engines/engine3_staleness   (uses ctx.learner)
        Engine 4  watch rules       detection/engines/engine4_watch_rules (uses ctx.fault_state)
        Engine 5  app health        detection/engines/engine5_apps        (uses ctx.app_health)
        Layer 4   suppression       detection/suppression.suppress_issues
        integration-name resolution + per-integration rollup (reporting/health)
      → VigilData
  6. learner bookkeeping            purge absent, flush, once-per-session recorder seed
  7. persist cross-cycle state      DowntimeRepo / RuleFaultRepo / AppHealthRepo .persist()
  8. drive the notification         reporting/notification.Notifier.update(...) (Layer 5)
  → return VigilData
```

`build_vigil_data` (`models.py`) partitions the suppressed issues into the
canonical buckets (`integration_failures`, `devices_offline`, `stale_devices`,
`device_faults`, `app_issues`), derives counts, and sets `healthy` (never
claimed during the startup grace). Every surface then reads that one payload
from `coordinator.data` — no consumer re-runs detection:

- **Persistent notification** with acknowledge semantics —
  `reporting/notification` (`Notifier` + `AckRepo`); dismissing acknowledges the
  shown issues until they clear and return.
- **Diagnostic sensors** — `sensor.py` (`CoordinatorEntity`s over
  `coordinator.data`).
- **"Clear acknowledgements" button** — `button.py`.
- **Dashboard card** — `frontend/vigil-card.js` reads `GET /api/vigil/state`
  (`http_api.py` → `reporting/serialize`).
- **Redacted diagnostics** — `diagnostics.py`.
