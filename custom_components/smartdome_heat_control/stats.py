"""Heizbericht: Tageswerte pro Raum sammeln und auswerten.

Reine Python-Logik ohne Home-Assistant-Abhängigkeit (testbar). Pro Raum und Tag
werden nur Sekunden bzw. Integrale aufsummiert – das bleibt auch über ein Jahr
klein und belastet die Datenbank nicht.

Idee und Aufbau angelehnt an das Statistikmodul von Goldi5/Raumklima-2 (MIT).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

# Faustregel (Verbraucherzentrale): ca. 6 % Heizenergie je Grad Absenkung.
ENERGY_PER_KELVIN = 0.06
MAX_DAYS = 400
# Toleranz „am Ziel“ in Kelvin
ON_TARGET_BAND = 1.0
# Unter dieser erfassten Zeit je Raum wird kein Prozentwert ausgegeben
MIN_REGULATED_S = 3600.0

ROOM_FIELDS = (
    "seconds",       # erfasste Zeit
    "heating_s",     # Status „heizt“
    "hold_s",        # Nachheizphase (residual_hold)
    "window_s",      # Heizung wegen offenem Fenster pausiert
    "setback_s",     # abgesenkt (Abwesenheit/Urlaub)
    "setback_ks",    # ∫ (Normal-Soll − Ist-Soll) K · s während der Absenkung
    "override_s",    # Komfort-Override aktiv
    "regulated_s",   # Zeit, in der geregelt wird (Bezug für on_target)
    "on_target_s",   # |Raum − Ziel| ≤ 1 K (oder wärmer ohne Heizen)
    "temp_ks",       # ∫ Temperatur · s (für den Mittelwert)
    "temp_n_s",      # Zeit mit gültiger Temperatur
    "fault_s",       # Raumsensor gestört
)


@dataclass
class RoomSample:
    """Momentaufnahme eines Raums (jede Minute)."""

    state: str                 # idle / heating / residual_hold / off
    target: float | None       # wirksame Zieltemperatur
    normal_target: float | None  # Ziel ohne Abwesenheit/Urlaub
    actual: float | None
    window_pause: bool = False
    setback: bool = False
    override: bool = False
    sensor_fault: bool = False


def add_sample(day: dict[str, float], s: RoomSample, dt: float) -> None:
    """Eine Messung (dt Sekunden) eines Raums aufsummieren."""
    for f in ROOM_FIELDS:
        day.setdefault(f, 0.0)
    day["seconds"] += dt
    if s.state == "heating":
        day["heating_s"] += dt
    elif s.state == "residual_hold":
        day["hold_s"] += dt
    if s.window_pause:
        day["window_s"] += dt
    if s.setback:
        day["setback_s"] += dt
        if s.target is not None and s.normal_target is not None:
            day["setback_ks"] += max(0.0, s.normal_target - s.target) * dt
    if s.override:
        day["override_s"] += dt
    if s.sensor_fault:
        day["fault_s"] += dt
    if s.actual is not None:
        day["temp_ks"] += s.actual * dt
        day["temp_n_s"] += dt
    if (
        s.actual is not None
        and s.target is not None
        and not s.window_pause
        and not s.sensor_fault
    ):
        day["regulated_s"] += dt
        if abs(s.actual - s.target) <= ON_TARGET_BAND or (
            s.actual > s.target and s.state != "heating"
        ):
            day["on_target_s"] += dt


class HeatingStats:
    """Tageswerte aller Räume, in einem JSON-tauglichen Dict gehalten."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        # {"2026-11-02": {"kinderzimmer": {...Felder...}}}
        self.days: dict[str, dict[str, dict[str, float]]] = {}
        if data and isinstance(data.get("days"), dict):
            for key, rooms in data["days"].items():
                if not isinstance(rooms, dict):
                    continue
                self.days[str(key)] = {
                    str(rid): {k: float(v) for k, v in vals.items()
                               if isinstance(v, (int, float))}
                    for rid, vals in rooms.items()
                    if isinstance(vals, dict)
                }

    def to_dict(self) -> dict[str, Any]:
        return {"days": self.days}

    def add(self, day: date, room_id: str, sample: RoomSample, dt: float) -> None:
        rooms = self.days.setdefault(day.isoformat(), {})
        add_sample(rooms.setdefault(room_id, {}), sample, dt)

    def prune(self, today: date) -> None:
        """Alles ab MAX_DAYS Tagen Alter entfernen."""
        limit = (today - timedelta(days=MAX_DAYS)).isoformat()
        for key in [k for k in self.days if k < limit]:
            del self.days[key]

    def forget_room(self, room_id: str) -> None:
        for rooms in self.days.values():
            rooms.pop(room_id, None)

    def sum_room(self, room_id: str, start: date, end: date) -> dict[str, float]:
        """Summe eines Raums über start..end (beide inklusive)."""
        total = {f: 0.0 for f in ROOM_FIELDS}
        d = start
        while d <= end:
            vals = self.days.get(d.isoformat(), {}).get(room_id)
            if vals:
                for f in ROOM_FIELDS:
                    total[f] += vals.get(f, 0.0)
            d += timedelta(days=1)
        return total

    def room_ids(self) -> set[str]:
        return {rid for rooms in self.days.values() for rid in rooms}


# ----------------------------------------------------------------- Auswertung
def hours(seconds: float) -> float:
    return round(seconds / 3600.0, 1)


def summarize(total: dict[str, float]) -> dict[str, Any]:
    """Aus Summen lesbare Kennzahlen eines Zeitraums machen."""
    sec = total["seconds"]
    out: dict[str, Any] = {
        "tracked_h": hours(sec),
        "heating_h": hours(total["heating_s"] + total["hold_s"]),
        "window_pause_h": hours(total["window_s"]),
        "setback_h": hours(total["setback_s"]),
        "override_h": hours(total["override_s"]),
        "fault_h": hours(total["fault_s"]),
        "avg_temperature": (
            round(total["temp_ks"] / total["temp_n_s"], 1)
            if total["temp_n_s"] > 0 else None
        ),
        "heating_share_pct": (
            round(100.0 * (total["heating_s"] + total["hold_s"]) / sec)
            if sec > 0 else None
        ),
        "on_target_pct": (
            round(100.0 * total["on_target_s"] / total["regulated_s"])
            if total["regulated_s"] >= MIN_REGULATED_S else None
        ),
        "avg_setback_k": (
            round(total["setback_ks"] / total["setback_s"], 1)
            if total["setback_s"] > 0 else None
        ),
        # Richtwert: Absenkung (Ø K) × 6 %/K, bezogen auf die Absenkzeit,
        # gewichtet mit ihrem Anteil an der Gesamtzeit
        "saving_pct": (
            round(
                100.0 * ENERGY_PER_KELVIN * total["setback_ks"] / sec, 1
            ) if sec > 0 and total["setback_s"] > 0 else None
        ),
    }
    return out


def build_report(
    stats: HeatingStats,
    labels: dict[str, str],
    start: date,
    end: date,
    prev_start: date | None = None,
    prev_end: date | None = None,
) -> dict[str, Any]:
    """Bericht für alle Räume im Zeitraum, optional mit Vorperiode."""
    rooms: list[dict[str, Any]] = []
    grand = {f: 0.0 for f in ROOM_FIELDS}
    prev_grand = {f: 0.0 for f in ROOM_FIELDS}
    for rid in labels:
        total = stats.sum_room(rid, start, end)
        for f in ROOM_FIELDS:
            grand[f] += total[f]
        item = {"room_id": rid, "label": labels[rid], **summarize(total)}
        if prev_start and prev_end:
            prev = stats.sum_room(rid, prev_start, prev_end)
            for f in ROOM_FIELDS:
                prev_grand[f] += prev[f]
            item["prev_heating_h"] = hours(prev["heating_s"] + prev["hold_s"])
        rooms.append(item)
    rooms.sort(key=lambda r: r["heating_h"], reverse=True)
    result: dict[str, Any] = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rooms": rooms,
        "total": summarize(grand),
    }
    if prev_start and prev_end:
        result["prev_heating_h"] = hours(
            prev_grand["heating_s"] + prev_grand["hold_s"]
        )
    return result


def format_report(report: dict[str, Any], title: str) -> str:
    """Kurzer Text für Push-Nachrichten."""
    total = report["total"]
    lines = [
        f"{title} ({report['start']} – {report['end']})",
        f"Heizzeit gesamt: {total['heating_h']} h",
    ]
    prev = report.get("prev_heating_h")
    if prev:
        diff = round(100.0 * (total["heating_h"] - prev) / prev)
        lines[-1] += f" ({diff:+d} % zur Vorperiode)"
    if total["saving_pct"] is not None:
        lines.append(f"Absenkung spart rund {total['saving_pct']} % (Richtwert)")
    if total["window_pause_h"]:
        lines.append(f"Fenster-Pausen: {total['window_pause_h']} h")
    lines.append("Räume nach Heizzeit:")
    for r in report["rooms"]:
        extra = (
            f", {r['on_target_pct']} % am Ziel"
            if r["on_target_pct"] is not None else ""
        )
        lines.append(f"• {r['label']}: {r['heating_h']} h{extra}")
    return "\n".join(lines)


def to_csv(report: dict[str, Any]) -> str:
    """Bericht als CSV (Semikolon, deutsches Excel)."""
    head = ["Raum", "Heizzeit_h", "Heizanteil_%", "Fenster_Pause_h",
            "Absenkung_h", "Absenkung_K", "Am_Ziel_%", "Mittel_C"]
    rows = [";".join(head)]
    for r in report["rooms"]:
        vals = [
            r["label"], r["heating_h"], r["heating_share_pct"],
            r["window_pause_h"], r["setback_h"], r["avg_setback_k"],
            r["on_target_pct"], r["avg_temperature"],
        ]
        rows.append(";".join("" if v is None else str(v).replace(".", ",")
                             for v in vals))
    return "\n".join(rows)
