"""Diagnostic sensors: how much garbage collection and event loop delay cost."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, PATCHES, STATUS_ACTIVE


@dataclass(frozen=True, kw_only=True)
class PerfSensorDescription(SensorEntityDescription):
    value: Callable[[dict[str, Any]], Any]
    attrs: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _active(data: dict[str, Any]) -> str:
    status = data.get("status", {})
    return f"{sum(1 for s, _ in status.values() if s == STATUS_ACTIVE)}/{len(PATCHES)}"


def _patch_attrs(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, (status, reason) in data.get("status", {}).items():
        out[name] = f"{status}: {reason}" if reason else status
    return out


SENSORS: tuple[PerfSensorDescription, ...] = (
    PerfSensorDescription(
        key="gc_share", translation_key="gc_share", native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1,
        value=lambda d: d["gc_share"]),
    PerfSensorDescription(
        key="gc_max_pause", translation_key="gc_max_pause",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
        value=lambda d: d["gc_max_pause"]),
    PerfSensorDescription(
        key="gc_full_collections", translation_key="gc_full_collections",
        state_class=SensorStateClass.MEASUREMENT, value=lambda d: d["gc_full_collections"]),
    PerfSensorDescription(
        key="loop_delay_max", translation_key="loop_delay_max",
        native_unit_of_measurement=UnitOfTime.MILLISECONDS,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
        value=lambda d: d["loop_delay_max"]),
    PerfSensorDescription(
        key="frozen_objects", translation_key="frozen_objects",
        value=lambda d: d["frozen_objects"]),
    PerfSensorDescription(
        key="active_fixes", translation_key="active_fixes", value=_active, attrs=_patch_attrs),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities(PerfSensor(coordinator, entry, d) for d in SENSORS)


class PerfSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry, description: PerfSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name="Performance Tune",
            manufacturer="perf_tune", entry_type=DeviceEntryType.SERVICE)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs is None:
            return None
        return self.entity_description.attrs(self.coordinator.data or {})
