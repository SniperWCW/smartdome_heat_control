"""Statistik der Fensterpausen mit geschätzter Gas-Ersparnis."""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import DOMAIN, SIGNAL_WINDOW_STATS_UPDATED

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.window_stats"
KEEP_DAYS = 400
HISTORY_DAYS = 14
SAVE_DELAY = 60


def heating_power_kw(
    *,
    area_m2: float,
    load_w_m2: float,
    indoor: float | None,
    outdoor: float | None,
    design_outdoor: float,
    fallback_kw: float,
) -> float:
    """Geschätzte Heizleistung eines Raums in kW.

    Mit Raumfläche: Auslegungsleistung (Fläche * W/m²), skaliert mit dem
    Verhältnis aus aktueller und Auslegungs-Temperaturdifferenz. Ohne
    Außentemperatur wird die halbe Auslegungsleistung angesetzt. Ohne
    Fläche gilt die feste Leistung ``fallback_kw``.
    """
    if area_m2 <= 0:
        return max(0.0, fallback_kw)
    design_kw = area_m2 * load_w_m2 / 1000.0
    if outdoor is None or indoor is None:
        return design_kw * 0.5
    span = indoor - design_outdoor
    if span <= 0:
        return 0.0
    factor = (indoor - outdoor) / span
    return design_kw * min(max(factor, 0.0), 1.5)


def parse_hhmm(value: Any) -> dtime | None:
    """'HH:MM' in time umwandeln, sonst None."""
    try:
        hh, mm = str(value).split(":")[:2]
        return dtime(int(hh), int(mm))
    except (ValueError, TypeError):
        return None


def in_time_window(now: dtime, start: dtime, end: dtime) -> bool:
    """Prüfen, ob now im Fenster liegt (auch über Mitternacht)."""
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def render_text(template: str, **values: Any) -> str:
    """Platzhalter {raum}, {minuten}, {fenster} ersetzen, Unbekanntes bleibt stehen."""
    out = str(template)
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


def split_services(value: Any) -> list[str]:
    """Liste oder kommagetrennten Text in notify-Dienste umwandeln."""
    if isinstance(value, str):
        items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = []
    result: list[str] = []
    for item in items:
        name = str(item).strip()
        if name.startswith("notify."):
            name = name[len("notify."):]
        if name and name not in result:
            result.append(name)
    return result


class WindowStats:
    """Hält Pausenminuten und gesparte Energie pro Raum und Tag."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._rooms: dict[str, dict[str, dict[str, float]]] = {}
        self._loaded = False

    async def async_load(self) -> None:
        if self._loaded:
            return
        data = await self._store.async_load()
        if isinstance(data, dict):
            rooms = data.get("rooms", {})
            if isinstance(rooms, dict):
                self._rooms = {
                    rid: {
                        day: {
                            "min": float(v.get("min", 0.0)),
                            "kwh": float(v.get("kwh", 0.0)),
                        }
                        for day, v in days.items()
                        if isinstance(v, dict)
                    }
                    for rid, days in rooms.items()
                    if isinstance(days, dict)
                }
        self._loaded = True

    def add_minute(self, room_id: str, kwh: float, day: date | None = None) -> None:
        """Eine Pausenminute samt gesparter Energie verbuchen."""
        day = day or datetime.now().date()
        days = self._rooms.setdefault(room_id, {})
        entry = days.setdefault(day.isoformat(), {"min": 0.0, "kwh": 0.0})
        entry["min"] += 1.0
        entry["kwh"] += max(0.0, kwh)
        self._prune(days, day)
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)
        async_dispatcher_send(self.hass, SIGNAL_WINDOW_STATS_UPDATED, room_id)

    def remove_room(self, room_id: str) -> None:
        if self._rooms.pop(room_id, None) is not None:
            self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    @staticmethod
    def _prune(days: dict[str, dict[str, float]], today: date) -> None:
        limit = (today - timedelta(days=KEEP_DAYS)).isoformat()
        for key in [k for k in days if k < limit]:
            days.pop(key, None)

    def _data_to_save(self) -> dict[str, Any]:
        return {"rooms": self._rooms}

    def summary(
        self,
        room_id: str,
        gas_kwh_per_m3: float,
        today: date | None = None,
    ) -> dict[str, Any]:
        """Zusammenfassung für heute, Monat, Jahr, gesamt und den Verlauf."""
        today = today or datetime.now().date()
        days = self._rooms.get(room_id, {})
        periods = {
            "today": lambda d: d == today,
            "month": lambda d: (d.year, d.month) == (today.year, today.month),
            "year": lambda d: d.year == today.year,
            "total": lambda d: True,
        }
        out: dict[str, Any] = {}
        for name, match in periods.items():
            minutes = kwh = 0.0
            for key, val in days.items():
                try:
                    d = date.fromisoformat(key)
                except ValueError:
                    continue
                if match(d):
                    minutes += val["min"]
                    kwh += val["kwh"]
            out[name] = {
                "minutes": round(minutes),
                "kwh": round(kwh, 2),
                "m3": round(kwh / gas_kwh_per_m3, 2) if gas_kwh_per_m3 > 0 else 0.0,
            }
        history = []
        for offset in range(HISTORY_DAYS - 1, -1, -1):
            d = today - timedelta(days=offset)
            val = days.get(d.isoformat(), {"min": 0.0})
            history.append({"date": d.isoformat(), "minutes": round(val["min"])})
        out["history"] = history
        return out
