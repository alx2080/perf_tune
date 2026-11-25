# Performance Tune

**English** | [Українська](README.uk.md)

A Home Assistant custom integration for systems with thousands of fast-changing entities (SCADA, Modbus/MQTT/S7 with thousands of points). It removes two causes of slowness:

1. **The browser cannot keep up with the stream of changes.** The UI receives every change of every entity as a separate message, whatever page is open. At hundreds of changes per second pages lag, and Home Assistant drops the connection ("Client unable to keep up with pending messages").
2. **The whole of Home Assistant freezes for 1–1.5 s from time to time.** This is Python's full garbage collection. Every state change leaves an Event ↔ Context reference cycle that only the full collection frees. paho-mqtt adds a cycle of its own for every message.

Measured on HAOS 2026.11.2, 3 vCPU: 15,000 points (Modbus + MQTT + S7), each changing once per second.

| | Without the integration | With the integration |
|---|---|---|
| UI | 6–10 fps, delay up to 2 s, disconnects | 32 fps, 5–50 ms |
| Core freezes ≥ 0.5 s | ~15 per minute, 1.3–1.6 s each | 0 (max 72 ms) |
| Time spent on garbage collection | ~35 % | ~3 % |
| Home Assistant core CPU | 25–34 % | 18–20 % |

## Fixes

Each one can be switched on or off in the integration options.

| Fix | What it does |
|---|---|
| **ws_batch** | Replaces the `subscribe_entities` WebSocket command. Changes are collected and sent to the browser in one message every 0.5 s, with only the latest state of each entity. The message format stays the same, the frontend is not changed. |
| **gc_freeze** | Runs `gc.collect(); gc.freeze()` one minute after start. About 2 million long-lived objects are no longer scanned by every full collection. |
| **break_cycles** | Clears `context.origin_event` after `state_changed` for entities with the given prefixes (`sensor.` by default). Event, Context and State are freed by reference counting, without a full collection. `origin_event` is used by the live logbook for "triggered by". Not all of the Home Assistant code was checked for other uses: if you see anything odd, switch this fix off first. |
| **patch_paho** | Replaces `paho.mqtt.matcher.MQTTMatcher.iter_match` with an equivalent version without a self-referencing closure. The results are compared on a set of topics before the replacement. |

## Safety across Home Assistant updates

The integration touches internals of Home Assistant and paho-mqtt. Therefore:

- **Check before switching on.** Each fix first checks that the functions and attributes it needs are there and look as expected.
- **If an update changed them,** the fix is **not switched on**. Home Assistant works as usual, a message appears under Settings → Repairs, and the reason is shown in the attributes of the "Active fixes" sensor.
- **Rollback.** Disabling or removing the integration puts everything back without a restart. Verified by reloading the integration; removal was not verified separately. Exception: browser tabs that are already open return to normal delivery after they reconnect.

## Diagnostic sensors

Device **Performance Tune**. The sensors update every 10 s and show figures for the last minute:
- **garbage collection:** share of time, longest pause, number of full collections;
- **event loop delay:** maximum;
- **number of frozen objects;**
- **active fixes:** `4/4`, with the status of each in the attributes.

Actions:
- `perf_tune.garbage_report` — which objects end up as cyclic garbage (to find the source of pauses);
- `perf_tune.freeze` — freeze objects now.

Both are for administrators only. The report is limited to 10 seconds, because garbage piles up in memory meanwhile.

## Installation

**HACS:** ⋮ → Custom repositories, repository `https://github.com/alx2080/perf_tune`, type Integration. Then **Download** and restart Home Assistant.

**Manually:** copy `custom_components/perf_tune` to `config/custom_components/` and restart Home Assistant.

Then Settings → Devices & services → Add integration → Performance Tune.

## What not to do

Do not raise the threshold of the full collection (`gc.set_threshold(…, …, 1000)`). The freezes disappear, but cyclic garbage starts to pile up: in the test the core's memory grew from 2 to 6.5 GB in 3 minutes.

## Limitations

- Tested only on Home Assistant 2026.11.2 on a test bench; there was no long run (a day or more).
- **ws_batch** relieves the browser, but drawing a page with thousands of values is still heavy. Use dashboards with 50–100 points.
- Above about 15,000 changes per second the single-threaded event loop of Home Assistant remains the limit. Fewer changes help: a deadband, rounding, slower polling.
