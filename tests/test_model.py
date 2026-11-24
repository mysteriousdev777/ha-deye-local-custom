"""The model as data: what may be written, how requests are laid out, decoding."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from decimal import Decimal

import pytest

from custom_components.deye_local_custom.const import (
    GRID_CHARGE_PRESETS_A,
    GRID_VOLTAGE_INTERVAL_S,
)
from custom_components.deye_local_custom.description import (
    MAX_REQUEST,
    clock_drift,
    combine,
    fault_codes,
    hex_code,
    number,
)
from custom_components.deye_local_custom.models import (
    MODELS,
    find_model,
    parse_info,
)
from custom_components.deye_local_custom.models.sg03lp1 import SG03LP1
from custom_components.deye_local_custom.planner import Request, plan
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.util import dt as dt_util

from .fake_logger import INVERTER_SERIAL, default_registers, s16

DEFAULT_KEYS = {
    d.key for d in SG03LP1.descriptions if d.entity_registry_enabled_default
}


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.key)
def test_layout(model) -> None:
    keys = [d.key for d in model.descriptions]
    assert len(keys) == len(set(keys))
    for d in model.descriptions:
        assert [b for b in model.blocks if b.holds(d)], f"{d.key}: in no block"
        assert len([b for b in model.blocks if b.holds(d)]) == 1, d.key
        assert d.interval % model.tick == 0
    for block in model.blocks:
        assert block.last - block.first + 1 <= MAX_REQUEST


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.key)
def test_only_declared_registers_and_values_are_writable(model) -> None:
    for d in model.selects:
        assert len(d.registers) == 1
        assert len(d.values) == len(d.options) == len(set(d.values))
        low, high = d.limits
        assert all(low <= v <= high for v in d.values), d.key
    assert model.writable == {
        d.registers[0]: frozenset(d.values) for d in model.selects
    }


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.key)
def test_display_precision_is_the_registers_resolution(model) -> None:
    """Every sensor with a unit states its display precision. Without one, Home
    Assistant takes a default of the device class: a current in A got 2 decimals,
    "50.00 A" for a register of whole amperes."""
    for d in model.sensors:
        if d.native_unit_of_measurement is None:
            continue
        assert d.suggested_display_precision is not None, d.key
        sample = d.decode([1] * len(d.registers))
        if sample is None:  # not a plain number (clock drift of an impossible date)
            continue
        decimals = max(0, -int(Decimal(str(sample)).normalize().as_tuple().exponent))
        assert d.suggested_display_precision == decimals, d.key


def test_sg03lp1_writes_only_the_grid_charging_presets() -> None:
    assert SG03LP1.writable == {230: frozenset(GRID_CHARGE_PRESETS_A)}
    (select,) = SG03LP1.selects
    assert select.options == [f"{a} A" for a in GRID_CHARGE_PRESETS_A]
    assert select.limits == (0, 120)


def test_grid_status_and_voltage_come_from_one_request() -> None:
    by_key = {d.key: d for d in SG03LP1.descriptions}
    voltage, status = by_key["grid_voltage_l1"], by_key["grid_connected_status"]
    assert voltage.interval == GRID_VOLTAGE_INTERVAL_S
    (block,) = [b for b in SG03LP1.blocks if b.holds(voltage)]
    assert block.holds(status)
    # Only grid status and voltage enabled: still one request for both
    assert plan(SG03LP1, {voltage.key, status.key}, {}, 0.0) == [Request(150, 45)]


def test_grid_status_never_without_grid_voltage() -> None:
    """Grid voltage at 5 s of its own: a refresh between polls that finds grid status
    due and grid voltage not still reads both (a block's request starts at its lowest
    active register)."""
    model = dataclasses.replace(
        SG03LP1,
        sensors=tuple(
            dataclasses.replace(d, interval=5) if d.key == "grid_voltage_l1" else d
            for d in SG03LP1.sensors
        ),
    )
    assert model.tick == 5
    read_at = dict.fromkeys(DEFAULT_KEYS, 0.0)
    assert plan(model, DEFAULT_KEYS, read_at, 5.0) == [Request(150, 1)]  # its step
    read_at = dict.fromkeys(DEFAULT_KEYS, 0.5)
    read_at["grid_voltage_l1"] = 6.5  # read alone at its step, 1.7 s ago
    assert plan(model, DEFAULT_KEYS, read_at, 8.2) == [Request(150, 45)]


def test_default_load_is_about_seven_requests_a_minute() -> None:
    read_at: dict[str, float] = {}
    requests: list[Request] = []
    for now in range(0, 60, 10):
        due = plan(SG03LP1, DEFAULT_KEYS, read_at, float(now))
        requests += due
        for d in SG03LP1.descriptions:
            if any(r.holds(d) for r in due):
                read_at[d.key] = float(now)
    assert requests[:2] == [Request(150, 81), Request(59, 33)]  # 150-230, 59-91
    assert requests[2:] == [Request(150, 45)] * 5
    # A minute later (polls drift late: 61 s)
    assert plan(SG03LP1, DEFAULT_KEYS, read_at, 111.0) == [
        Request(150, 81),
        Request(59, 33),
    ]


def test_a_failed_request_is_retried_on_the_next_poll() -> None:
    read_at = {d.key: 0.0 for d in SG03LP1.descriptions}
    read_at["running_status"] = -60.0  # due at 0, failed: still due 10 s later
    assert Request(59, 1) in plan(SG03LP1, {"running_status"}, read_at, 10.0)


def test_enabled_entities_widen_or_add_requests() -> None:
    keys = DEFAULT_KEYS | {"fault_codes", "clock_drift"}
    assert plan(SG03LP1, keys, {}, 0.0) == [
        Request(150, 81),
        Request(59, 48),  # up to 106
        Request(22, 3),
    ]
    assert plan(SG03LP1, set(), {}, 0.0) == []


def test_previous_entities_keep_their_names() -> None:
    """Device "Inverter" + these names = the entity ids of the previous integration."""
    names = {d.key: d.name for d in SG03LP1.descriptions}
    previous = {
        "running_status": "Running Status",
        "total_battery_charge": "Total Battery Charge",
        "total_battery_discharge": "Total Battery Discharge",
        "grid_frequency": "Grid Frequency",
        "total_load_consumption": "Total Load Consumption",
        "dc_temperature": "DC Temperature",
        "ac_temperature": "AC Temperature",
        "grid_voltage_l1": "Grid Voltage L1",
        "load_voltage": "Load Voltage",
        "total_grid_power": "Total Grid Power",
        "total_power": "Total Power",
        "load_l1_power": "Load L1 Power",
        "total_load_power": "Total Load Power",
        "battery_voltage": "Battery Voltage",
        "battery_soc": "Battery SOC",
        "battery_power": "Battery Power",
        "battery_current": "Battery Current",
        "load_frequency": "Load Frequency",
        "grid_connected_status": "Grid-connected Status",
    }
    assert {k: names[k] for k in previous} == previous
    assert previous.keys() <= DEFAULT_KEYS


def test_enum_states() -> None:
    by_key = {d.key: d for d in SG03LP1.sensors}
    status = by_key["grid_connected_status"]
    assert status.device_class is SensorDeviceClass.ENUM
    assert status.options == ["Off-Grid", "On-Grid"]
    assert [status.decode([v]) for v in (0, 1, 2)] == ["Off-Grid", "On-Grid", None]
    running = by_key["running_status"]
    assert running.options == ["Stand-by", "Self-checking", "Normal", "FAULT"]
    assert [running.decode([v]) for v in range(6)] == [
        "Stand-by",
        "Self-checking",
        "Normal",
        "FAULT",
        "FAULT",
        None,
    ]


def test_decoding_the_snapshot() -> None:
    registers = default_registers()
    values = {
        d.key: d.decode([registers.get(r, 0) for r in d.registers])
        for d in SG03LP1.descriptions
        if d.key != "clock_drift"
    }
    assert values["grid_voltage_l1"] == 230.5
    assert values["battery_power"] == -1570
    assert values["battery_current"] == -29.5
    assert values["battery_voltage"] == 53.21
    assert values["total_battery_charge"] == 7456.5
    assert values["total_energy_bought"] == 8767.2  # registers 78 and 80
    assert values["grid_frequency"] == 50.03
    assert values["dc_temperature"] == 35.0
    assert values["battery_temperature"] == 21.5
    assert values["grid_charging_current"] == 50
    assert values["grid_charge"] is True
    assert values["fault_codes"] == "OK"
    assert values["warning_code"] == "OK"


def test_numbers() -> None:
    assert combine([0xFFFF], signed=True) == -1
    assert combine([0x0001, 0x8000], signed=True) == -(1 << 31) + 1
    assert combine([0x5678, 0x1234]) == 0x12345678
    assert number(0.1)([2305]) == 230.5
    assert number(0.01, signed=True)([s16(-2950)]) == -29.5
    assert number()([42]) == 42 and isinstance(number()([42]), int)
    assert number(1.0)([42]) == 42 and isinstance(number(1.0)([42]), int)
    assert number(0.1, signed=True, offset=1000)([950]) == -5.0


def test_codes() -> None:
    assert fault_codes([0, 0, 0, 0]) == "OK"
    # bit 12 -> F13, bit 34 -> F35 (third register, bit 2)
    assert fault_codes([1 << 12, 0, 1 << 2, 0]) == "F13, F35"
    assert hex_code([0, 0]) == "OK"
    assert hex_code([0x0002, 0x0001]) == "0x00010002"


def test_clock_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 11, 24, 12, 0, 0, tzinfo=dt_util.get_default_time_zone())
    monkeypatch.setattr(dt_util, "now", lambda: now)
    assert clock_drift([26 << 8 | 11, 24 << 8 | 12, 0 << 8 | 30]) == 30
    assert clock_drift([26 << 8 | 11, 24 << 8 | 11, 59 << 8 | 0]) == -60
    assert clock_drift([26 << 8 | 13, 24 << 8 | 12, 0]) is None  # month 13


def test_info_and_model_lookup() -> None:
    registers = default_registers()
    info = parse_info([registers.get(r, 0) for r in range(18)])
    assert (info.device_type, info.serial, info.rated_power) == (
        3,
        INVERTER_SERIAL,
        5000.0,
    )
    assert find_model(0x0003) is SG03LP1
    assert find_model(0x0300) is SG03LP1
    assert find_model(0x0103) is None
    assert find_model(0x0006) is None
    with pytest.raises(ValueError):
        parse_info([0] * 18)
