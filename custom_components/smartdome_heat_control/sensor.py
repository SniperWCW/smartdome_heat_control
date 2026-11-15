"""Sensor-Entities für Smartdome Heat Control."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ROOMS,
    CONF_ROOM_LABEL,
    DATA_CONTROLLER,
    DOMAIN,
    SIGNAL_ROOM_STATE_UPDATED,
    SIGNAL_WINDOW_STATS_UPDATED,
)

WINDOW_PAUSE_PERIODS = {
    "today": "Fensterpause heute",
    "month": "Fensterpause Monat",
    "year": "Fensterpause Jahr",
    "total": "Fensterpause gesamt",
}

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
        for period, period_label in WINDOW_PAUSE_PERIODS.items():
            entities.append(
                SmartdomeWindowPauseSensor(hass, entry, room_id, label, period, period_label)
            )

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
            "outdoor_cutoff_active": status["outdoor_cutoff_active"],
            "outdoor_minimum_active": status["outdoor_minimum_active"],
            "control_target_temperature": status["control_target_temperature"],
            "sensor_fault": status["sensor_fault"],
            "control_reason": status["control_reason"],
            "comfort_override_active": status["comfort_override_active"],
            "comfort_override_target": status["comfort_override_target"],
            "comfort_override_remaining_seconds": status["comfort_override_remaining_seconds"],
            "window_open": status.get("window_open", False),
            "window_pause_active": status.get("window_pause_active", False),
            "thermostats": status.get("thermostats", []),
            **self._window_stats_attributes(controller),
        }

    def _window_stats_attributes(self, controller: Any) -> dict[str, Any]:
        """Pausenstatistik als flache Attribute (für Dashboards wie Lumora)."""
        summary = controller.get_window_summary(self._room_id)
        attrs: dict[str, Any] = {}
        for period in ("today", "month", "year", "total"):
            attrs[f"window_pause_{period}_min"] = summary[period]["minutes"]
            attrs[f"gas_saved_{period}_m3"] = summary[period]["m3"]
            attrs[f"gas_saved_{period}_kwh"] = summary[period]["kwh"]
        attrs["window_pause_history"] = [d["minutes"] for d in summary["history"]]
        return attrs

    async def async_added_to_hass(self) -> None:
        """Auf Raumzustands-Updates reagieren, ohne zu pollen."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_ROOM_STATE_UPDATED,
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_WINDOW_STATS_UPDATED,
                self._handle_stats_update,
            )
        )

    @callback
    def _handle_stats_update(self, room_id: str) -> None:
        if room_id == self._room_id:
            self.async_write_ha_state()

    @callback
    def _handle_update(self, room_states: dict[str, Any]) -> None:
        # room_states enthält nur aktive Räume – bei einem deaktivierten Raum
        # (state wechselt auf "off") ist dieser Raum hier NICHT enthalten,
        # daher unabhängig vom Payload-Inhalt immer aktualisieren.
        self.async_write_ha_state()


class SmartdomeWindowPauseSensor(SensorEntity):
    """Pausenzeit der Heizung bei offenem Fenster mit geschätzter Gas-Ersparnis."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:window-open-variant"
    _attr_native_unit_of_measurement = "min"
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        room_id: str,
        room_label: str,
        period: str,
        period_label: str,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._room_id = room_id
        self._period = period
        self._attr_unique_id = f"{entry.entry_id}_room_{room_id}_window_pause_{period}"
        self._attr_name = f"{room_label} – {period_label}"
        if period == "total":
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def available(self) -> bool:
        return self._entry.entry_id in self.hass.data.get(DOMAIN, {})

    def _summary(self) -> dict[str, Any] | None:
        entry_data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        controller = entry_data.get(DATA_CONTROLLER) if entry_data else None
        if controller is None:
            return None
        return controller.get_window_summary(self._room_id)

    @property
    def native_value(self) -> int | None:
        summary = self._summary()
        return None if summary is None else summary[self._period]["minutes"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        summary = self._summary()
        if summary is None:
            return {}
        period = summary[self._period]
        attrs: dict[str, Any] = {
            "gespartes_gas_m3": period["m3"],
            "gespartes_gas_kwh": period["kwh"],
        }
        if self._period == "today":
            attrs["verlauf"] = summary["history"]
        return attrs

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_WINDOW_STATS_UPDATED, self._handle_update
            )
        )

    @callback
    def _handle_update(self, room_id: str) -> None:
        if room_id == self._room_id:
            self.async_write_ha_state()
