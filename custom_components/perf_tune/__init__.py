"""Performance Tune: keep Home Assistant responsive with thousands of fast-changing entities.

Fixes (each can be switched off in the options, each checks the internals it
needs first and is skipped with a repair issue when a Home Assistant update
changed them):

* ws_batch     — browsers get state changes in one message every N seconds
                 instead of one message per change;
* gc_freeze    — gc.freeze() some time after start: long-lived objects are no
                 longer scanned by every full garbage collection;
* break_cycles — no Event <-> Context reference cycle per state change of the
                 chosen entities, so they are freed without the full collection;
* patch_paho   — paho-mqtt topic matching without a self-referencing closure
                 per incoming message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform, __version__ as HA_VERSION
from homeassistant.core import (
    CoreState,
    HomeAssistant,
    ServiceCall,
    SupportsResponse,
    callback,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

try:  # Home Assistant 2026.x moved from voluptuous to probatio
    import probatio as vol
except ImportError:  # pragma: no cover
    import voluptuous as vol

from .const import (
    CONF_BREAK_CYCLES,
    CONF_CYCLE_PREFIXES,
    CONF_GC_FREEZE,
    CONF_GC_FREEZE_DELAY,
    CONF_GC_WARN_MS,
    CONF_PATCH_PAHO,
    CONF_WS_BATCH,
    CONF_WS_INTERVAL,
    DEFAULTS,
    DOMAIN,
    PATCH_BREAK_CYCLES,
    PATCH_GC_FREEZE,
    PATCH_PAHO,
    PATCH_WS_BATCH,
    MAX_REPORT_SECONDS,
    PATCHES,
    SENSOR_INTERVAL,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    STATUS_INCOMPATIBLE,
    STATUS_PENDING,
)
from .patches import (
    CycleBreakerPatch,
    GcMonitor,
    PahoPatch,
    PatchUnavailable,
    WsBatchPatch,
    async_garbage_report,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]

type PerfTuneConfigEntry = ConfigEntry[PerfTuneRuntime]


@dataclass
class PerfTuneRuntime:
    monitor: GcMonitor
    coordinator: DataUpdateCoordinator
    patches: dict[str, Any] = field(default_factory=dict)
    status: dict[str, tuple[str, str]] = field(default_factory=dict)
    cancel_freeze: Any = None


def option(entry: ConfigEntry, key: str) -> Any:
    return entry.options.get(key, DEFAULTS[key])


def parse_prefixes(text: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in str(text).replace(",", "\n").splitlines() if p.strip())


def _set_status(hass: HomeAssistant, rt: PerfTuneRuntime, name: str, status: str,
                reason: str = "") -> None:
    rt.status[name] = (status, reason)
    issue_id = f"incompatible_{name}"
    if status == STATUS_INCOMPATIBLE:
        _LOGGER.warning("perf_tune: %s not applied (%s) on Home Assistant %s",
                        name, reason, HA_VERSION)
        ir.async_create_issue(
            hass, DOMAIN, issue_id, is_fixable=False, is_persistent=False,
            severity=ir.IssueSeverity.WARNING, translation_key="incompatible",
            translation_placeholders={"patch": name, "reason": reason, "version": HA_VERSION},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


async def async_setup_entry(hass: HomeAssistant, entry: PerfTuneConfigEntry) -> bool:
    monitor = GcMonitor(hass, float(option(entry, CONF_GC_WARN_MS)))
    monitor.start()

    rt: PerfTuneRuntime  # assigned below; the coordinator reads it lazily

    async def _update() -> dict[str, Any]:
        data = monitor.snapshot()
        data["status"] = dict(rt.status)
        return data

    coordinator = DataUpdateCoordinator(
        hass, _LOGGER, config_entry=entry, name=DOMAIN, update_method=_update,
        update_interval=timedelta(seconds=SENSOR_INTERVAL),
    )
    rt = PerfTuneRuntime(monitor=monitor, coordinator=coordinator)
    entry.runtime_data = rt

    def _try(name: str, enabled: bool, patch) -> None:
        if not enabled:
            _set_status(hass, rt, name, STATUS_DISABLED)
            return
        try:
            patch.apply()
        except PatchUnavailable as err:
            _set_status(hass, rt, name, STATUS_INCOMPATIBLE, str(err))
            return
        except Exception as err:  # noqa: BLE001 - never break Home Assistant
            _LOGGER.exception("perf_tune: %s failed", name)
            _set_status(hass, rt, name, STATUS_INCOMPATIBLE, repr(err))
            return
        rt.patches[name] = patch
        _set_status(hass, rt, name, STATUS_ACTIVE)

    _try(PATCH_WS_BATCH, option(entry, CONF_WS_BATCH),
         WsBatchPatch(hass, float(option(entry, CONF_WS_INTERVAL))))
    _try(PATCH_BREAK_CYCLES, option(entry, CONF_BREAK_CYCLES),
         CycleBreakerPatch(hass, parse_prefixes(option(entry, CONF_CYCLE_PREFIXES))))
    _try(PATCH_PAHO, option(entry, CONF_PATCH_PAHO), PahoPatch())

    # gc.freeze() once the system has settled after start.
    if option(entry, CONF_GC_FREEZE):
        _set_status(hass, rt, PATCH_GC_FREEZE, STATUS_PENDING)
        delay = float(option(entry, CONF_GC_FREEZE_DELAY))

        @callback
        def _do_freeze(_now=None) -> None:
            rt.cancel_freeze = None
            result = monitor.freeze()
            _LOGGER.info("perf_tune: gc.freeze() — %s objects in %s ms",
                         result["frozen_objects"], result["took_ms"])
            _set_status(hass, rt, PATCH_GC_FREEZE, STATUS_ACTIVE)

        @callback
        def _schedule(_event=None) -> None:
            rt.cancel_freeze = async_call_later(hass, delay, _do_freeze)

        if hass.state is CoreState.running:
            _schedule()
        else:
            rt.cancel_freeze = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _schedule)
    else:
        _set_status(hass, rt, PATCH_GC_FREEZE, STATUS_DISABLED)

    _register_services(hass)
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PerfTuneConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    rt = entry.runtime_data
    if rt.cancel_freeze:
        rt.cancel_freeze()
    for name, patch in rt.patches.items():
        try:
            patch.revert()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("perf_tune: reverting %s failed", name)
    if rt.status.get(PATCH_GC_FREEZE, ("",))[0] == STATUS_ACTIVE:
        rt.monitor.unfreeze()
    rt.monitor.stop()
    for name in PATCHES:
        ir.async_delete_issue(hass, DOMAIN, f"incompatible_{name}")
    for service in ("garbage_report", "freeze"):
        hass.services.async_remove(DOMAIN, service)
    return ok


def _register_services(hass: HomeAssistant) -> None:
    def _runtime() -> PerfTuneRuntime | None:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        return entries[0].runtime_data if entries else None

    async def _garbage_report(call: ServiceCall) -> dict[str, Any]:
        return await async_garbage_report(call.data["seconds"])

    async def _freeze(call: ServiceCall) -> dict[str, Any]:
        result = GcMonitor.freeze()
        if (rt := _runtime()) is not None:
            _set_status(hass, rt, PATCH_GC_FREEZE, STATUS_ACTIVE)
        return result

    # Administrators only: both block Home Assistant for a moment, and the
    # report keeps all cyclic garbage in memory while it runs.
    async_register_admin_service(
        hass, DOMAIN, "garbage_report", _garbage_report,
        schema=vol.Schema({vol.Optional("seconds", default=2): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=MAX_REPORT_SECONDS))}),
        supports_response=SupportsResponse.ONLY)
    async_register_admin_service(
        hass, DOMAIN, "freeze", _freeze, schema=vol.Schema({}),
        supports_response=SupportsResponse.OPTIONAL)
