"""Tests für die reine Logik in window_stats.py (ohne Home Assistant)."""
import importlib.util
import sys
import types
from datetime import date, time
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "smartdome_heat_control"


def _load():
    """window_stats isoliert laden, ohne die Stubs anderer Tests zu stören."""
    from unittest.mock import patch

    class Store:
        def __init__(self, *a):
            pass

        def async_delay_save(self, *a):
            pass

    def stub(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        return mod

    helpers = stub("homeassistant.helpers")
    helpers.__path__ = []
    fakes = {
        "homeassistant": stub("homeassistant"),
        "homeassistant.core": stub("homeassistant.core", HomeAssistant=object),
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.dispatcher": stub(
            "homeassistant.helpers.dispatcher", async_dispatcher_send=lambda *a: None
        ),
        "homeassistant.helpers.storage": stub("homeassistant.helpers.storage", Store=Store),
    }
    pkg = types.ModuleType("smartdome_pkg")
    pkg.__path__ = [str(PKG)]
    modules = {}
    with patch.dict(sys.modules, {**fakes, "smartdome_pkg": pkg}):
        for mod in ("const", "window_stats"):
            spec = importlib.util.spec_from_file_location(f"smartdome_pkg.{mod}", PKG / f"{mod}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"smartdome_pkg.{mod}"] = module
            spec.loader.exec_module(module)
            modules[mod] = module
    return modules["window_stats"]


w = _load()


def test_power_scales_with_temperature_difference():
    kw = dict(area_m2=20, load_w_m2=60, design_outdoor=-12, fallback_kw=1)
    assert w.heating_power_kw(indoor=21, outdoor=-12, **kw) == 1.2
    assert abs(w.heating_power_kw(indoor=21, outdoor=4.5, **kw) - 0.6) < 1e-9
    assert w.heating_power_kw(indoor=21, outdoor=25, **kw) == 0
    assert w.heating_power_kw(indoor=21, outdoor=None, **kw) == 0.6


def test_fixed_power_without_area():
    assert w.heating_power_kw(
        area_m2=0, load_w_m2=60, indoor=21, outdoor=0, design_outdoor=-12, fallback_kw=1.5
    ) == 1.5


def test_lock_window_over_midnight():
    assert w.in_time_window(time(23), time(22), time(6))
    assert w.in_time_window(time(5), time(22), time(6))
    assert not w.in_time_window(time(12), time(22), time(6))
    assert not w.in_time_window(time(12), time(8), time(8))


def test_text_and_services():
    assert w.render_text("{raum} {minuten} {x}", raum="Bad", minuten=5) == "Bad 5 {x}"
    assert w.split_services("notify.a, b\nc,a") == ["a", "b", "c"]


def test_summary_periods_and_history():
    stats = w.WindowStats(object())
    today = date(2026, 11, 15)
    for _ in range(30):
        stats.add_minute("r", 0.02, today)
    stats.add_minute("r", 0.5, date(2026, 10, 1))
    s = stats.summary("r", 10.0, today)
    assert s["today"]["minutes"] == 30
    assert s["month"]["minutes"] == 30
    assert s["year"]["minutes"] == 31
    assert s["total"]["kwh"] == 1.1
    assert len(s["history"]) == 14 and s["history"][-1]["minutes"] == 30
