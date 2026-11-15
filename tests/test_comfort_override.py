"""Tests für den befristeten Komfort-Override (Fork-Funktion)."""

from __future__ import annotations

from conftest import const_module as const


def _ctrl(make_controller):
    ctrl = make_controller({"bad": {const.CONF_ROOM_THERMOSTAT: "climate.bad_valve"}})
    ctrl._comfort_override = {}
    ctrl._comfort_override_seconds = 3600
    return ctrl


def test_override_gilt_befristet(make_controller, clock):
    ctrl = _ctrl(make_controller)
    ctrl.set_comfort_override("bad", 23.0)
    assert ctrl._active_comfort_target("bad") == 23.0
    clock.advance(3599)
    assert ctrl._active_comfort_target("bad") == 23.0
    clock.advance(2)
    assert ctrl._active_comfort_target("bad") is None


def test_override_wird_begrenzt_und_nur_fuer_aktive_raeume_gesetzt(make_controller):
    ctrl = _ctrl(make_controller)
    ctrl.set_comfort_override("bad", 99)
    assert ctrl._active_comfort_target("bad") == 30.0
    ctrl.set_comfort_override("unbekannt", 22)
    assert ctrl._active_comfort_target("unbekannt") is None
