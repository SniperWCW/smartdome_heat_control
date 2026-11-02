"""Sensor-Entities für Smartdome Heat Control."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTime
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .stats import build_report, summarize

from .const import (
    CONF_ROOMS,
    CONF_ROOM_LABEL,
    DATA_CONTROLLER,
    DOMAIN,
    SIGNAL_ROOM_STATE_UPDATED,
)

ROOM_STATUS_OPTIONS = ["off", "idle", "heating", "residual_hold", "window_pause"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sensor-Entities für einen Config Entry anlegen."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    cfg = entry_data.get("config", {})
    rooms = cfg.get(CONF_ROOMS, {})

    entities: list[SensorEntity] = []
    for room_id, room in rooms.items():
        if not isinstance(room, dict):
            continue
        label = str(room.get(CONF_ROOM_LABEL, room_id))
        entities.append(SmartdomeRoomStatusSensor(hass, entry, room_id, label))
        entities.append(SmartdomeRoomHeatingHoursSensor(hass, entry, room_id, label))

    entities.append(SmartdomeHeatingReportSensor(hass, entry))

    async_add_entities(entities, True)


class SmartdomeRoomStatusSensor(SensorEntity):
    """Zustand eines Raums mit Zieltemperatur, Ist-Temperatur und Heizmodus als Attribute."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ROOM_STATUS_OPTIONS
    _attr_icon = "mdi:home-thermometer"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        room_id: str,
        room_label: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._room_id = room_id
        self._attr_unique_id = f"{entry.entry_id}_room_{room_id}_status"
        self._attr_name = f"{room_label} – Status"

    @property
    def available(self) -> bool:
        """Entity ist verfügbar, solange der Config Entry geladen ist."""
        return self._entry.entry_id in self.hass.data.get(DOMAIN, {})

    def _get_controller(self) -> Any:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if entry_data is None:
            return None
        return entry_data.get(DATA_CONTROLLER)

    @property
    def native_value(self) -> str | None:
        controller = self._get_controller()
        if controller is None:
            return None
        return controller.get_room_status(self._room_id)["state"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        controller = self._get_controller()
        if controller is None:
            return {}
        status = controller.get_room_status(self._room_id)
        return {
            "target_temperature": status["target_temperature"],
            "current_temperature": status["current_temperature"],
            "heating_mode": status["heating_mode"],
            "enabled": status["enabled"],
            "away_active": status["away_active"],
            "control_reason": status.get("control_reason"),
            "control_target_temperature": status.get("control_target_temperature"),
            "outdoor_cutoff_active": status.get("outdoor_cutoff_active"),
            "outdoor_minimum_active": status.get("outdoor_minimum_active"),
            "sensor_fault": status.get("sensor_fault"),
            "comfort_override_active": status.get("comfort_override_active", False),
            "comfort_override_target": status.get("comfort_override_target"),
            "comfort_override_remaining_seconds": status.get(
                "comfort_override_remaining_seconds", 0
            ),
        }

    async def async_added_to_hass(self) -> None:
        """Auf Raumzustands-Updates reagieren, ohne zu pollen."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_ROOM_STATE_UPDATED,
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self, room_states: dict[str, Any]) -> None:
        # room_states enthält nur aktive Räume – bei einem deaktivierten Raum
        # (state wechselt auf "off") ist dieser Raum hier NICHT enthalten,
        # daher unabhängig vom Payload-Inhalt immer aktualisieren.
        self.async_write_ha_state()


class _StatsSensorBase(SensorEntity):
    """Gemeinsame Basis der Heizbericht-Sensoren."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry

    @property
    def available(self) -> bool:
        return self._entry.entry_id in self.hass.data.get(DOMAIN, {})

    def _get_controller(self) -> Any:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        return None if entry_data is None else entry_data.get(DATA_CONTROLLER)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_ROOM_STATE_UPDATED, self._handle_update
            )
        )

    @callback
    def _handle_update(self, room_states: dict[str, Any]) -> None:
        self.async_write_ha_state()


class SmartdomeRoomHeatingHoursSensor(_StatsSensorBase):
    """Heizzeit eines Raums in den letzten 7 Tagen (heute/30 Tage als Attribute)."""

    _attr_icon = "mdi:radiator"

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, room_id: str, room_label: str
    ) -> None:
        super().__init__(hass, entry)
        self._room_id = room_id
        self._attr_unique_id = f"{entry.entry_id}_room_{room_id}_heating_hours"
        self._attr_name = f"{room_label} – Heizzeit 7 Tage"

    def _period(self, days: int) -> dict[str, Any] | None:
        controller = self._get_controller()
        if controller is None:
            return None
        today = dt_util.now().date()
        total = controller.stats.sum_room(
            self._room_id, today - timedelta(days=days - 1), today
        )
        return summarize(total)

    @property
    def native_value(self) -> float | None:
        data = self._period(7)
        return None if data is None else data["heating_h"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        today, week, month = self._period(1), self._period(7), self._period(30)
        if today is None or week is None or month is None:
            return {}
        return {
            "heating_hours_today": today["heating_h"],
            "heating_hours_30d": month["heating_h"],
            "heating_share_pct_7d": week["heating_share_pct"],
            "window_pause_hours_7d": week["window_pause_h"],
            "setback_hours_7d": week["setback_h"],
            "avg_setback_k_7d": week["avg_setback_k"],
            "on_target_pct_7d": week["on_target_pct"],
            "avg_temperature_7d": week["avg_temperature"],
            "comfort_override_hours_7d": week["override_h"],
            "sensor_fault_hours_7d": week["fault_h"],
        }


class SmartdomeHeatingReportSensor(_StatsSensorBase):
    """Heizzeit aller Räume in den letzten 7 Tagen mit Rangliste als Attribut."""

    _attr_icon = "mdi:chart-bar"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry)
        self._attr_unique_id = f"{entry.entry_id}_heating_report"
        self._attr_name = "Heizbericht 7 Tage"

    def _report(self) -> dict[str, Any] | None:
        controller = self._get_controller()
        if controller is None:
            return None
        rooms = controller.config.get(CONF_ROOMS, {})
        labels = {
            rid: str(r.get(CONF_ROOM_LABEL, rid))
            for rid, r in rooms.items()
            if isinstance(r, dict)
        }
        today = dt_util.now().date()
        return build_report(
            controller.stats, labels, today - timedelta(days=6), today
        )

    @property
    def native_value(self) -> float | None:
        report = self._report()
        return None if report is None else report["total"]["heating_h"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        report = self._report()
        if report is None:
            return {}
        total = report["total"]
        return {
            "saving_pct": total["saving_pct"],
            "window_pause_hours": total["window_pause_h"],
            "ranking": [
                {"room": r["label"], "heating_hours": r["heating_h"],
                 "on_target_pct": r["on_target_pct"]}
                for r in report["rooms"]
            ],
        }
