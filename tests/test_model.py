"""The model as data: what may be written, how requests are laid out, decoding."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import itertools

import pytest

from custom_components.deye_local_custom.const import (
    DEFAULT_INTERVALS,
    GRID_CHARGE_PRESETS_A,
    INTERVAL_CHOICES,
    Tier,
)
from custom_components.deye_local_custom.description import (
    MAX_REQUEST,
    clock_drift,
    combine,
    fault_codes,
    hex_code,
    interval_errors,
    intervals_from,
    intervals_in_use,
    number,
)
from custom_components.deye_local_custom.models import (
    MODELS,
    find_model,
    parse_info,
)
from custom_components.deye_local_custom.models.sg03lp1 import CLOCK, SG03LP1
from custom_components.deye_local_custom.planner import (
    Request,
    plan,
    requests_per_minute,
)
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.util import dt as dt_util

from .fake_logger import INVERTER_SERIAL, default_registers, s16

DEFAULT_KEYS = {
    d.key for d in SG03LP1.descriptions if d.entity_registry_enabled_default
}
VOLTAGE, FAST, SLOW = Tier.GRID_VOLTAGE, Tier.FAST, Tier.SLOW


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
    assert (voltage.tier, status.tier) == (VOLTAGE, FAST)
    (block,) = [b for b in SG03LP1.blocks if b.holds(voltage)]
    assert block.holds(status)
    # Only grid status and voltage enabled: still one request for both
    assert plan(SG03LP1, {voltage.key, status.key}, {}, 0.0) == [Request(150, 45)]


def test_grid_status_never_without_grid_voltage() -> None:
    """Grid voltage at 5 s of its own: a refresh between polls that finds grid status
    due and grid voltage not still reads both (a block's request starts at its lowest
    active register)."""
    model = SG03LP1.with_intervals({VOLTAGE: 5})
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


def test_tiers() -> None:
    """Grid voltage alone in its tier; the clock the only fixed interval; each tier
    read at its default."""
    tiers = {d.key: d.tier for d in SG03LP1.descriptions}
    assert [k for k, t in tiers.items() if t is VOLTAGE] == ["grid_voltage_l1"]
    assert [k for k, t in tiers.items() if t is None] == ["clock_drift"]
    assert tiers["grid_charging_current_preset"] is SLOW
    for d in SG03LP1.descriptions:
        assert d.interval == (DEFAULT_INTERVALS[d.tier] if d.tier else CLOCK), d.key
    assert SG03LP1.with_intervals(DEFAULT_INTERVALS) == SG03LP1
    assert interval_errors(DEFAULT_INTERVALS) == {}


def test_interval_choices_keep_the_step() -> None:
    """The checks interval_errors leaves out hold for every choice."""
    for fast, slow in itertools.product(INTERVAL_CHOICES[FAST], INTERVAL_CHOICES[SLOW]):
        assert slow % fast == 0, (fast, slow)
    for voltage in INTERVAL_CHOICES[VOLTAGE]:
        assert CLOCK % voltage == 0, voltage
    for tier in Tier:
        assert DEFAULT_INTERVALS[tier] in INTERVAL_CHOICES[tier]


@pytest.mark.parametrize("model", MODELS, ids=lambda m: m.key)
def test_every_valid_set_of_intervals(model) -> None:
    """The polling step is the grid voltage's interval, every interval a multiple."""
    valid = 0
    for choice in itertools.product(*(INTERVAL_CHOICES[tier] for tier in Tier)):
        intervals = dict(zip(Tier, choice, strict=True))
        if errors := interval_errors(intervals):
            assert errors == {FAST: "not_a_multiple_of_grid_voltage"}
            assert intervals[FAST] % intervals[VOLTAGE]
            continue
        valid += 1
        changed = model.with_intervals(intervals)
        assert changed.tick == intervals[VOLTAGE]
        for d, before in zip(changed.descriptions, model.descriptions, strict=True):
            assert d.interval == (intervals[d.tier] if d.tier else before.interval)
            assert d.interval % changed.tick == 0, d.key
    assert valid == 16 * 3  # grid voltage and fast pairs, any slow


def test_interval_errors() -> None:
    assert interval_errors({VOLTAGE: 10, FAST: 20, SLOW: 120}) == {}
    assert interval_errors({VOLTAGE: 10, FAST: 15, SLOW: 60}) == {
        FAST: "not_a_multiple_of_grid_voltage"
    }
    assert interval_errors({VOLTAGE: 20, FAST: 30, SLOW: 60}) == {
        FAST: "not_a_multiple_of_grid_voltage"
    }
    assert interval_errors({VOLTAGE: 7, FAST: 10, SLOW: 60}) == {
        VOLTAGE: "not_a_choice"
    }
    assert interval_errors({VOLTAGE: 5, FAST: 10}) == {SLOW: "not_a_choice"}


def test_intervals_from_options() -> None:
    assert intervals_from({}) == DEFAULT_INTERVALS
    assert intervals_from({"fast_interval": 20, "other": 1}) == {
        VOLTAGE: 10,
        FAST: 20,
        SLOW: 60,
    }
    stored = {
        "grid_voltage_interval": None,
        "fast_interval": "abc",
        "slow_interval": "60",
    }
    assert intervals_from(stored) == {VOLTAGE: None, FAST: None, SLOW: 60}
    assert interval_errors(intervals_from(stored)) == {
        VOLTAGE: "not_a_choice",
        FAST: "not_a_choice",
    }
    assert intervals_in_use(stored) == DEFAULT_INTERVALS
    assert intervals_in_use({"grid_voltage_interval": 5}) == {
        VOLTAGE: 5,
        FAST: 10,
        SLOW: 60,
    }


@pytest.mark.parametrize(
    ("intervals", "expected"),
    [
        ({}, 7.0),  # the defaults: 10 / 10 / 60
        ({VOLTAGE: 5}, 13.0),  # grid voltage alone between the fast requests
        ({FAST: 20}, 7.0),  # as many requests, half of them one register
        ({VOLTAGE: 15, FAST: 15}, 5.0),
        ({VOLTAGE: 20, FAST: 20, SLOW: 120}, 3.5),
        ({VOLTAGE: 30, FAST: 60, SLOW: 300}, 2.2),
    ],
)
def test_requests_a_minute(intervals: dict[Tier, int], expected: float) -> None:
    """About 60 / grid voltage + 60 / slow: every poll sends a request."""
    model = SG03LP1.with_intervals(intervals)
    assert requests_per_minute(model, DEFAULT_KEYS) == expected


def test_grid_voltage_alone_between_the_fast_requests() -> None:
    model = SG03LP1.with_intervals({FAST: 20})
    read_at: dict[str, float] = {}
    requests: list[Request] = []
    for now in range(0, 50, 10):
        due = plan(model, DEFAULT_KEYS, read_at, float(now))
        requests += due
        for d in model.descriptions:
            if any(r.holds(d) for r in due):
                read_at[d.key] = float(now)
    assert requests == [
        Request(150, 81),  # 0 s: with the settings
        Request(59, 33),
        Request(150, 1),  # 10 s: grid voltage alone
        Request(150, 45),  # 20 s
        Request(150, 1),
        Request(150, 45),  # 40 s
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
