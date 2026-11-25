"""Config flow for Performance Tune: one entry, all settings in the options."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

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
)


def _number(min_: float, max_: float, step: float, unit: str) -> NumberSelector:
    return NumberSelector(NumberSelectorConfig(min=min_, max=max_, step=step,
                                               unit_of_measurement=unit,
                                               mode=NumberSelectorMode.BOX))


def options_schema(o: dict[str, Any]) -> vol.Schema:
    def d(key: str) -> Any:
        return o.get(key, DEFAULTS[key])

    return vol.Schema({
        vol.Required(CONF_WS_BATCH, default=d(CONF_WS_BATCH)): BooleanSelector(),
        vol.Required(CONF_WS_INTERVAL, default=d(CONF_WS_INTERVAL)): vol.All(
            _number(0.1, 5, 0.1, "s"), vol.Coerce(float)),
        vol.Required(CONF_GC_FREEZE, default=d(CONF_GC_FREEZE)): BooleanSelector(),
        vol.Required(CONF_GC_FREEZE_DELAY, default=d(CONF_GC_FREEZE_DELAY)): vol.All(
            _number(0, 3600, 1, "s"), vol.Coerce(int)),
        vol.Required(CONF_BREAK_CYCLES, default=d(CONF_BREAK_CYCLES)): BooleanSelector(),
        vol.Required(CONF_CYCLE_PREFIXES, default=d(CONF_CYCLE_PREFIXES)): TextSelector(
            TextSelectorConfig(multiline=True)),
        vol.Required(CONF_PATCH_PAHO, default=d(CONF_PATCH_PAHO)): BooleanSelector(),
        vol.Required(CONF_GC_WARN_MS, default=d(CONF_GC_WARN_MS)): vol.All(
            _number(50, 10000, 10, "ms"), vol.Coerce(int)),
    })


class PerfTuneConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add Performance Tune (single instance)."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PerfTuneOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Performance Tune", data={},
                                           options={**DEFAULTS, **user_input})
        return self.async_show_form(step_id="user", data_schema=options_schema({}))


class PerfTuneOptionsFlow(OptionsFlow):
    """Change which fixes are on."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(step_id="init",
                                    data_schema=options_schema(dict(self.config_entry.options)))
