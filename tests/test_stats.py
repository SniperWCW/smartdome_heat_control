"""Tests für den Heizbericht (reine Logik, ohne Home Assistant)."""
import importlib.util
from datetime import date, timedelta
from pathlib import Path

_p = Path(__file__).parent.parent / "custom_components/smartdome_heat_control/stats.py"
_spec = importlib.util.spec_from_file_location("sdhc_stats", _p)
stats = importlib.util.module_from_spec(_spec)
import sys; sys.modules["sdhc_stats"] = stats
_spec.loader.exec_module(stats)

D = date(2026, 11, 2)


def sample(**kw):
    base = dict(state="idle", target=21.0, normal_target=21.0, actual=21.0)
    base.update(kw)
    return stats.RoomSample(**base)


def feed(st, day, room, s, minutes):
    for _ in range(minutes):
        st.add(day, room, s, 60.0)


def test_heating_hours_and_hold_counted():
    st = stats.HeatingStats()
    feed(st, D, "kiz", sample(state="heating", actual=19.0), 90)
    feed(st, D, "kiz", sample(state="residual_hold"), 30)
    feed(st, D, "kiz", sample(), 60)
    rep = stats.summarize(st.sum_room("kiz", D, D))
    assert rep["heating_h"] == 2.0
    assert rep["tracked_h"] == 3.0
    assert rep["heating_share_pct"] == 67


def test_window_pause_excluded_from_target_time():
    st = stats.HeatingStats()
    feed(st, D, "r", sample(window_pause=True, actual=17.0), 120)
    feed(st, D, "r", sample(), 120)
    rep = stats.summarize(st.sum_room("r", D, D))
    assert rep["window_pause_h"] == 2.0
    assert rep["on_target_pct"] == 100  # nur geregelte Zeit zählt


def test_on_target_needs_enough_data():
    st = stats.HeatingStats()
    feed(st, D, "r", sample(), 30)
    assert stats.summarize(st.sum_room("r", D, D))["on_target_pct"] is None


def test_on_target_band_and_warmer_without_heating():
    st = stats.HeatingStats()
    feed(st, D, "r", sample(actual=19.5, state="heating"), 60)   # 1,5 K zu kalt
    feed(st, D, "r", sample(actual=23.0), 60)                    # wärmer, kein Heizen
    feed(st, D, "r", sample(actual=21.4), 60)                    # im Band
    rep = stats.summarize(st.sum_room("r", D, D))
    assert rep["on_target_pct"] == 67


def test_setback_saving_estimate():
    st = stats.HeatingStats()
    # 12 h um 4 K abgesenkt von 24 h: 6 % * 4 K * 50 % = 12 %
    feed(st, D, "r", sample(setback=True, target=17.0, normal_target=21.0, actual=18.0), 720)
    feed(st, D, "r", sample(), 720)
    rep = stats.summarize(st.sum_room("r", D, D))
    assert rep["avg_setback_k"] == 4.0
    assert rep["saving_pct"] == 12.0


def test_sensor_fault_not_regulated():
    st = stats.HeatingStats()
    feed(st, D, "r", sample(sensor_fault=True, actual=None), 120)
    rep = stats.summarize(st.sum_room("r", D, D))
    assert rep["fault_h"] == 2.0 and rep["avg_temperature"] is None


def test_roundtrip_and_prune():
    st = stats.HeatingStats()
    feed(st, D, "a", sample(state="heating"), 10)
    old = D - timedelta(days=stats.MAX_DAYS + 5)
    feed(st, old, "a", sample(), 5)
    st.prune(D)
    assert old.isoformat() not in st.days
    st2 = stats.HeatingStats(st.to_dict())
    assert st2.sum_room("a", D, D)["heating_s"] == 600.0


def test_corrupt_store_is_ignored():
    st = stats.HeatingStats({"days": {"2026-11-02": {"a": "kaputt"}, "x": 5}})
    assert st.sum_room("a", D, D)["seconds"] == 0.0


def test_report_ranking_prev_and_text_csv():
    st = stats.HeatingStats()
    for i in range(7):
        feed(st, D - timedelta(days=i), "bad", sample(state="heating"), 120)
        feed(st, D - timedelta(days=i), "kiz", sample(state="heating"), 60)
        feed(st, D - timedelta(days=7 + i), "bad", sample(state="heating"), 60)
    labels = {"kiz": "Kinderzimmer", "bad": "Bad", "leer": "Büro"}
    rep = stats.build_report(
        st, labels, D - timedelta(days=6), D,
        D - timedelta(days=13), D - timedelta(days=7),
    )
    assert [r["room_id"] for r in rep["rooms"]][:2] == ["bad", "kiz"]
    assert rep["total"]["heating_h"] == 21.0
    assert rep["prev_heating_h"] == 7.0
    text = stats.format_report(rep, "Heizbericht Woche")
    assert "+200 %" in text and "Bad: 14.0 h" in text
    csv = stats.to_csv(rep)
    assert csv.splitlines()[0].startswith("Raum;") and "Bad;14,0" in csv
