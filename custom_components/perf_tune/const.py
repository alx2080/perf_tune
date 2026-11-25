"""Constants for Performance Tune."""

from typing import Final

DOMAIN: Final = "perf_tune"

# Options
CONF_WS_BATCH: Final = "ws_batch"
CONF_WS_INTERVAL: Final = "ws_batch_interval"
CONF_GC_FREEZE: Final = "gc_freeze"
CONF_GC_FREEZE_DELAY: Final = "gc_freeze_delay"
CONF_BREAK_CYCLES: Final = "break_cycles"
CONF_CYCLE_PREFIXES: Final = "break_cycles_prefixes"
CONF_PATCH_PAHO: Final = "patch_paho"
CONF_GC_WARN_MS: Final = "gc_warn_ms"

DEFAULTS: Final = {
    CONF_WS_BATCH: True,
    CONF_WS_INTERVAL: 0.5,
    CONF_GC_FREEZE: True,
    CONF_GC_FREEZE_DELAY: 60,
    CONF_BREAK_CYCLES: True,
    CONF_CYCLE_PREFIXES: "sensor.",
    CONF_PATCH_PAHO: True,
    CONF_GC_WARN_MS: 500,
}

# Patch names (also issue ids and status keys)
PATCH_WS_BATCH: Final = "ws_batch"
PATCH_GC_FREEZE: Final = "gc_freeze"
PATCH_BREAK_CYCLES: Final = "break_cycles"
PATCH_PAHO: Final = "patch_paho"
PATCHES: Final = (PATCH_WS_BATCH, PATCH_GC_FREEZE, PATCH_BREAK_CYCLES, PATCH_PAHO)

STATUS_ACTIVE: Final = "active"
STATUS_PENDING: Final = "pending"
STATUS_DISABLED: Final = "disabled"
STATUS_INCOMPATIBLE: Final = "incompatible"

WINDOW_SECONDS: Final = 60
SENSOR_INTERVAL: Final = 10
MAX_REPORT_SECONDS: Final = 10
