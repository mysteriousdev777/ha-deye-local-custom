"""Deye SUN-5K-SG03LP1-EU: single-phase low-voltage hybrid, through a Solarman logger.

Addresses, scales and signs agree in three sources: StephanJoubert's and davidrapan's
deye_hybrid.yaml and kellerza/sunsynk's single_phase.py (where they differ in sign only,
signed is right: the values are the same within the range in use).

Request layout: the fast block (150-194: grid, load, battery, status) every 10 s in one
request, the settings (210-232) with it once a minute; the slow block (59-91: counters,
temperatures, running status, grid frequency) once a minute. About 7 requests a minute
with the default entities. Disabled entities are not read; entities outside the default
ranges widen a request or add one (clock: 22-24).

Names: the entities of the previous integration (StephanJoubert's solarman) keep its
names, so they get the same entity ids under the device "Inverter" and their history
goes on. New entities have Home Assistant style names.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)

from ..const import GRID_CHARGE_PRESETS_A, GRID_VOLTAGE_INTERVAL_S
from ..description import (
    Block,
    Decoder,
    DeyeBinarySensorDescription,
    DeyeSelectDescription,
    DeyeSensorDescription,
    Model,
    bit,
    clock_drift,
    fault_codes,
    hex_code,
    lookup,
    number,
)

FAST = 10  # seconds
SLOW = 60
CLOCK = 300

# Register 59. The previous integration knew 0-3 and named 3 FAULT; the other sources
# call 3 "alarm" and 4 "fault". Both show as FAULT: the fault and warning code sensors
# tell them apart.
RUNNING_STATUS = {
    0: "Stand-by",
    1: "Self-checking",
    2: "Normal",
    3: "FAULT",
    4: "FAULT",
}
# Register 194: whether the inverter works with the grid (its grid relay), not whether
# the grid is there: it stays Off-Grid for a while after the grid comes back, and the
# inverter may leave a grid it finds unstable.
GRID_STATUS = {0: "Off-Grid", 1: "On-Grid"}

# Icons of states other than the usual one (the default icon is in icons.json, which
# only takes lowercase state keys: these keep the previous integration's spelling).
RUNNING_STATUS_ICONS = {
    "Stand-by": "mdi:sleep",
    "Self-checking": "mdi:progress-wrench",
    "FAULT": "mdi:alert-circle",
}
GRID_STATUS_ICONS = {"Off-Grid": "mdi:transmission-tower-off"}

V = UnitOfElectricPotential.VOLT
A = UnitOfElectricCurrent.AMPERE
W = UnitOfPower.WATT
KWH = UnitOfEnergy.KILO_WATT_HOUR
HZ = UnitOfFrequency.HERTZ
CELSIUS = UnitOfTemperature.CELSIUS

MEASUREMENT = SensorStateClass.MEASUREMENT
TOTAL_INCREASING = SensorStateClass.TOTAL_INCREASING
DIAGNOSTIC = EntityCategory.DIAGNOSTIC

TEMPERATURE = number(0.1, signed=True, offset=1000)  # tenths of a degree, + 100 degrees


def _sensor(
    key: str,
    name: str,
    registers: tuple[int, ...],
    decode: Decoder,
    interval: int,
    device_class: SensorDeviceClass | None = None,
    unit: str | None = None,
    *,
    state_class: SensorStateClass | None = MEASUREMENT,
    precision: int | None = None,
    enabled: bool = True,
    category: EntityCategory | None = None,
) -> DeyeSensorDescription:
    return DeyeSensorDescription(
        key=key,
        translation_key=key,
        name=name,
        registers=registers,
        decode=decode,
        interval=interval,
        device_class=device_class,
        native_unit_of_measurement=unit,
        state_class=state_class,
        suggested_display_precision=precision,
        entity_registry_enabled_default=enabled,
        entity_category=category,
    )


def _enum(
    key: str,
    name: str,
    register: int,
    states: dict[int, str],
    interval: int,
    icons: dict[str, str],
) -> DeyeSensorDescription:
    return DeyeSensorDescription(
        key=key,
        translation_key=key,
        name=name,
        registers=(register,),
        decode=lookup(states),
        interval=interval,
        device_class=SensorDeviceClass.ENUM,
        options=list(dict.fromkeys(states.values())),
        state_icons=icons,
    )


def _energy(
    key: str, name: str, registers: tuple[int, ...], *, enabled: bool = True
) -> DeyeSensorDescription:
    return _sensor(
        key,
        name,
        registers,
        number(0.1),
        SLOW,
        SensorDeviceClass.ENERGY,
        KWH,
        state_class=TOTAL_INCREASING,
        precision=1,
        enabled=enabled,
    )


POWER = SensorDeviceClass.POWER
VOLTAGE = SensorDeviceClass.VOLTAGE
CURRENT = SensorDeviceClass.CURRENT
FREQUENCY = SensorDeviceClass.FREQUENCY

SENSORS: tuple[DeyeSensorDescription, ...] = (
    # Grid
    _sensor(
        "grid_voltage_l1",
        "Grid Voltage L1",
        (150,),
        number(0.1),
        GRID_VOLTAGE_INTERVAL_S,
        VOLTAGE,
        V,
        precision=1,
    ),
    _enum(
        "grid_connected_status",
        "Grid-connected Status",
        194,
        GRID_STATUS,
        FAST,
        GRID_STATUS_ICONS,
    ),
    _sensor(
        "total_grid_power",
        "Total Grid Power",
        (169,),
        number(signed=True),
        FAST,
        POWER,
        W,
    ),
    _sensor(
        "grid_frequency",
        "Grid Frequency",
        (79,),
        number(0.01),
        SLOW,
        FREQUENCY,
        HZ,
        precision=2,
    ),
    _sensor(
        "grid_current",
        "Grid current",
        (160,),
        number(0.01, signed=True),
        FAST,
        CURRENT,
        A,
        precision=2,
        enabled=False,
    ),
    _sensor(
        "internal_ct_power",
        "Internal CT power",
        (167,),
        number(signed=True),
        FAST,
        POWER,
        W,
        enabled=False,
    ),
    _energy("daily_energy_bought", "Daily energy bought", (76,), enabled=False),
    _energy("daily_energy_sold", "Daily energy sold", (77,), enabled=False),
    # 79 between the words is the grid frequency
    _energy("total_energy_bought", "Total energy bought", (78, 80), enabled=False),
    _energy("total_energy_sold", "Total energy sold", (81, 82), enabled=False),
    # Load (the inverter's output to the house)
    _sensor(
        "load_voltage",
        "Load Voltage",
        (157,),
        number(0.1),
        FAST,
        VOLTAGE,
        V,
        precision=1,
    ),
    _sensor(
        "load_l1_power", "Load L1 Power", (176,), number(signed=True), FAST, POWER, W
    ),
    _sensor(
        "total_load_power",
        "Total Load Power",
        (178,),
        number(signed=True),
        FAST,
        POWER,
        W,
    ),
    _sensor(
        "load_frequency",
        "Load Frequency",
        (192,),
        number(0.01),
        FAST,
        FREQUENCY,
        HZ,
        precision=2,
    ),
    _energy("total_load_consumption", "Total Load Consumption", (85, 86)),
    _energy("daily_load_consumption", "Daily load consumption", (84,), enabled=False),
    # Inverter output
    _sensor("total_power", "Total Power", (175,), number(signed=True), FAST, POWER, W),
    _sensor(
        "output_power",
        "Output power",
        (173,),
        number(signed=True),
        FAST,
        POWER,
        W,
        enabled=False,
    ),
    _sensor(
        "output_current",
        "Output current",
        (164,),
        number(0.01, signed=True),
        FAST,
        CURRENT,
        A,
        precision=2,
        enabled=False,
    ),
    # Battery (no BMS link: the inverter measures it itself)
    _sensor(
        "battery_voltage",
        "Battery Voltage",
        (183,),
        number(0.01),
        FAST,
        VOLTAGE,
        V,
        precision=2,
    ),
    _sensor(
        "battery_soc",
        "Battery SOC",
        (184,),
        number(),
        FAST,
        SensorDeviceClass.BATTERY,
        PERCENTAGE,
    ),
    _sensor(
        "battery_power",
        "Battery Power",
        (190,),
        number(signed=True),
        FAST,
        POWER,
        W,
    ),
    _sensor(
        "battery_current",
        "Battery Current",
        (191,),
        number(0.01, signed=True),
        FAST,
        CURRENT,
        A,
        precision=2,
    ),
    _sensor(
        "battery_temperature",
        "Battery temperature",
        (182,),
        TEMPERATURE,
        FAST,
        SensorDeviceClass.TEMPERATURE,
        CELSIUS,
        precision=1,
        enabled=False,
    ),
    _energy("total_battery_charge", "Total Battery Charge", (72, 73)),
    _energy("total_battery_discharge", "Total Battery Discharge", (74, 75)),
    _energy("daily_battery_charge", "Daily battery charge", (70,), enabled=False),
    _energy("daily_battery_discharge", "Daily battery discharge", (71,), enabled=False),
    # PV (two MPPT inputs)
    _sensor("pv1_power", "PV1 power", (186,), number(), FAST, POWER, W, enabled=False),
    _sensor("pv2_power", "PV2 power", (187,), number(), FAST, POWER, W, enabled=False),
    _sensor(
        "pv1_voltage",
        "PV1 voltage",
        (109,),
        number(0.1),
        SLOW,
        VOLTAGE,
        V,
        precision=1,
        enabled=False,
    ),
    _sensor(
        "pv1_current",
        "PV1 current",
        (110,),
        number(0.1),
        SLOW,
        CURRENT,
        A,
        precision=1,
        enabled=False,
    ),
    _sensor(
        "pv2_voltage",
        "PV2 voltage",
        (111,),
        number(0.1),
        SLOW,
        VOLTAGE,
        V,
        precision=1,
        enabled=False,
    ),
    _sensor(
        "pv2_current",
        "PV2 current",
        (112,),
        number(0.1),
        SLOW,
        CURRENT,
        A,
        precision=1,
        enabled=False,
    ),
    # Inverter state
    _enum(
        "running_status",
        "Running Status",
        59,
        RUNNING_STATUS,
        SLOW,
        RUNNING_STATUS_ICONS,
    ),
    _sensor(
        "dc_temperature",
        "DC Temperature",
        (90,),
        TEMPERATURE,
        SLOW,
        SensorDeviceClass.TEMPERATURE,
        CELSIUS,
        precision=1,
    ),
    _sensor(
        "ac_temperature",
        "AC Temperature",
        (91,),
        TEMPERATURE,
        SLOW,
        SensorDeviceClass.TEMPERATURE,
        CELSIUS,
        precision=1,
    ),
    _sensor(
        "fault_codes",
        "Fault codes",
        (103, 104, 105, 106),
        fault_codes,
        SLOW,
        state_class=None,
        enabled=False,
        category=DIAGNOSTIC,
    ),
    _sensor(
        "warning_code",
        "Warning code",
        (101, 102),
        hex_code,
        SLOW,
        state_class=None,
        enabled=False,
        category=DIAGNOSTIC,
    ),
    _sensor(
        "clock_drift",
        "Clock drift",
        (22, 23, 24),
        clock_drift,
        CLOCK,
        unit=UnitOfTime.SECONDS,
        enabled=False,
        category=DIAGNOSTIC,
    ),
    # Settings, read only. The grid charging current is also the preset select below;
    # this sensor shows any value, also one set on the inverter's screen.
    _sensor(
        "grid_charging_current",
        "Grid charging current",
        (230,),
        number(),
        SLOW,
        CURRENT,
        A,
        category=DIAGNOSTIC,
    ),
    _sensor(
        "max_charging_current",
        "Max charging current",
        (210,),
        number(),
        SLOW,
        CURRENT,
        A,
        enabled=False,
        category=DIAGNOSTIC,
    ),
    _sensor(
        "max_discharging_current",
        "Max discharging current",
        (211,),
        number(),
        SLOW,
        CURRENT,
        A,
        enabled=False,
        category=DIAGNOSTIC,
    ),
)

BINARY_SENSORS: tuple[DeyeBinarySensorDescription, ...] = (
    DeyeBinarySensorDescription(
        key="grid_charge",
        translation_key="grid_charge",
        name="Grid charge",  # the battery may charge from the grid
        registers=(232,),
        decode=bit(0),
        interval=SLOW,
        entity_category=DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)

SELECTS: tuple[DeyeSelectDescription, ...] = (
    DeyeSelectDescription(
        key="grid_charging_current_preset",
        translation_key="grid_charging_current_preset",
        name="Grid charging current preset",
        registers=(230,),
        decode=number(),
        interval=SLOW,
        values=GRID_CHARGE_PRESETS_A,
        options=[f"{amperes} A" for amperes in GRID_CHARGE_PRESETS_A],
        # The SUN-5K's maximum battery charging current (datasheet): no preset above it
        limits=(0, 120),
        entity_category=EntityCategory.CONFIG,
    ),
)

SG03LP1 = Model(
    key="sg03lp1",
    manufacturer="Deye",
    name="SUN-5K-SG03LP1-EU",
    device_name="Inverter",
    # "Single-phase hybrid" in davidrapan's and kellerza's definitions (davidrapan:
    # 0x0003 or 0x0300; kellerza also 0x0103, which davidrapan calls off-grid: not taken)
    device_types=frozenset({0x0003, 0x0300}),
    blocks=(Block(150, 232), Block(59, 112), Block(22, 24)),
    sensors=SENSORS,
    binary_sensors=BINARY_SENSORS,
    selects=SELECTS,
)
