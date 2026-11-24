# Deye Local Custom

> [!IMPORTANT]
> A specialized custom integration for **one** inverter: the **Deye SUN-5K-SG03LP1-EU**,
> read through its **Solarman LSW-3** Wi-Fi logger. It refuses other device types and
> will not suit most users. For other Deye, Sunsynk or Solarman-logged inverters, use a
> general integration such as [davidrapan/ha-solarman](https://github.com/davidrapan/ha-solarman)
> or the [kellerza/sunsynk](https://github.com/kellerza/sunsynk) add-on.

A Home Assistant integration that polls the inverter over the local network (Modbus RTU
inside Solarman V5 frames, TCP port 8899 of the logger) and writes exactly one setting:
the battery's grid charging current, to one of two fixed presets.

<details>
<summary>Goals, and why the existing integrations did not fit</summary>

Goals:

- Values every 10 seconds without overloading a slow logger: about 7 requests a minute,
  with a request layout made for this inverter.
- One thing to write: the grid charging current, to fixed presets, confirmed by reading
  it back. No way to write any other register or value.
- No unavailable values during the logger's planned reboot (every 3 hours, 30-45 s),
  but honest unavailability when the inverter is really gone.
- Asynchronous.
- The entity ids and states of the previous integration, so that history, energy
  statistics and automations carry on unchanged (`On-Grid` / `Off-Grid` and so on).

The existing options:

- StephanJoubert's `home_assistant_solarman` (the previous integration here): archived;
  polls synchronously; registers services that write any register with any value.
- The `solarman` integration in Home Assistant Core: for Solarman's own devices (plugs,
  meters); it does not support inverters.
- davidrapan's `ha-solarman`: its services that write any register are always
  registered and available to any Home Assistant user; it manages the logger through
  the logger's web interface; values become unavailable after the second failed poll
  in a row.
- kellerza's `sunsynk`: an add-on that needs an MQTT broker (two more components to
  keep); its scheduler is made for RS485 and needs tuning for a logger; it is installed
  as a prebuilt image (not built from source on the host), so the image itself has to
  be checked; its `config.yaml` asks for the Supervisor role `homeassistant`, which
  allows restarting, stopping and updating Home Assistant Core.

</details>

## Install

Requires Home Assistant 2026.10.4 or newer. Home Assistant installs the requirements
itself: `pysolarmanv5==3.0.6` and `umodbus==1.0.4`.

First give the logger a fixed address in your router: the integration keeps the address
it was added with.

[![Open in the Marketplace or HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mysteriousdev777&repository=ha-deye-local-custom&category=integration)

The button opens this repository in the Marketplace (Home Assistant 2026.11 or newer) or
in HACS (older versions). Then go on from **Install** or **Download** in the steps below.

### Home Assistant 2026.11 or newer: Marketplace

1. **Settings → Marketplace → Add from link** (top-right).
2. Enter `mysteriousdev777/ha-deye-local-custom` and click **Add**; if asked for **Type**,
   choose **Integration**. The first time, Home Assistant asks you to connect GitHub: open
   the link it shows and enter the code there.
3. Open **Deye Local Custom** in the list, click **Install** and **Install** again to
   confirm.
4. Restart Home Assistant: **Settings → ⋮** (top-right) **→ Restart Home Assistant**.

### Older Home Assistant: HACS

1. Make sure [HACS](https://hacs.xyz/) is installed.
2. Open **HACS** in the Home Assistant sidebar, then the **⋮** menu in the top-right
   corner → **Custom repositories**.
3. In **Repository** enter `https://github.com/mysteriousdev777/ha-deye-local-custom`, as
   **Type** choose **Integration**, click **Add** and close the dialog.
4. Search for **Deye Local Custom** in HACS, open it, click **Download** in the
   bottom-right corner and **Download** again in the dialog.
5. Restart Home Assistant: **Settings → ⋮** (top-right) **→ Restart Home Assistant**.

### Add the inverter

[![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=deye_local_custom)

1. **Settings → Devices & services → Add integration** (bottom-right) → **Deye Local
   Custom**.
2. Enter the logger's details:
   - **Logger IP address**: your router's list of connected devices shows it.
   - **Port**: 8899 unless changed.
   - **Logger serial number**: of the logger (on its label and in its web page), not of
     the inverter. With a wrong serial number the logger does not answer at all.
3. The integration reads the inverter's device type and serial number; a device type
   other than single-phase hybrid is refused. Home Assistant shows the new device,
   **Inverter**, where you can pick an area. Its name gives the entity IDs:
   `sensor.inverter_grid_voltage_l1`, `sensor.inverter_battery_soc` and so on.

A new address later: **Reconfigure** in the integration's **⋮** menu changes the address,
port or logger serial number of the same inverter.

## Entities

Enabled by default (the names of the previous integration, for the same entity ids):

| Entity | Register(s) | Read every |
|---|---|---|
| Grid Voltage L1 | 150 | 10 s |
| Grid-connected Status (`Off-Grid` / `On-Grid`) | 194 | 10 s |
| Total Grid Power, Load L1 Power, Total Load Power, Total Power | 169, 176, 178, 175 | 10 s |
| Load Voltage, Load Frequency | 157, 192 | 10 s |
| Battery Voltage, Battery SOC, Battery Power, Battery Current | 183, 184, 190, 191 | 10 s |
| Running Status (`Stand-by`, `Self-checking`, `Normal`, `FAULT`) | 59 | 60 s |
| Grid Frequency, DC Temperature, AC Temperature | 79, 90, 91 | 60 s |
| Total Battery Charge, Total Battery Discharge, Total Load Consumption | 72-75, 85-86 | 60 s |
| Grid charging current (sensor: any value, also one set on the inverter's screen) | 230 | 60 s |
| Grid charging current preset (select: 30 A / 50 A) | 230 | 60 s |
| Connection (on while the values are fresh) | - | - |

Disabled by default, read only when enabled: daily counters (battery charge and
discharge, energy bought and sold, load), total energy bought and sold, grid current,
output power and current, internal CT power, battery temperature, PV1/PV2 power,
voltage and current, fault codes, warning code, clock drift (inverter vs Home
Assistant), max charging and discharging current, grid charge on/off.

Grid-connected Status is the state of the inverter's grid relay, not the presence of
grid voltage: after the grid comes back it stays `Off-Grid` for a while, and the
inverter may leave a grid it considers unstable.

## How it polls

- Fast values (registers 150-194) every 10 s in one request; the settings (210-232) join
  that request once a minute; the slow values (59-91) once a minute in another. About 7
  requests a minute with the default entities. Grid status and grid voltage always come
  from the same request.
- A disabled entity is not read: requests are planned from the enabled entities, so
  disabling the ones you do not need can shorten or drop a request.
- One TCP connection, one request at a time; reopened after a failure.
- A value stays available for its interval + 90 s after its last good read (fast values:
  100 s), and a failed request is retried on the next poll. The logger's planned reboot
  does not make anything unavailable; a real loss does, about 100 s after the last good
  read, when the Connection sensor also goes off. One warning in the log then, and one
  info line when the inverter answers again.
- After a Home Assistant restart the entities start with fresh values; while the logger
  is away, set-up is retried.

## Writing the grid charging current

Only through the **Grid charging current preset** select, and only its values: 30 A and
50 A (register 230, which limits charging from the grid; solar charging is not limited by
it). The values are a constant in the code (`GRID_CHARGE_PRESETS_A`), kept within the
inverter's limits by a test. There are no services and no other writable entities.

Each attempt reads the register first (nothing is written if it already holds the
value), writes it, and reads it back; the state always comes from the inverter. While
the logger does not answer, the next attempt comes 10 s after a failed one, the last one
starting within 60 s, and polls wait meanwhile. The action waits for the result: it
fails if the inverter refuses the value, keeps another one, or does not answer in time.

## Not included

- Writing anything else; services for raw register access.
- Managing the logger (its web interface, cloud or access point settings).
- Network discovery of the logger.
- Other inverter models: the code keeps a model as data (registers, decoding, intervals,
  writable registers with their values), but only this one is described and tested.

## Development

```sh
pip install -r requirements-dev.txt
ruff format . && ruff check . && pytest
python tools/privacy_check.py
```

The tests run against a fake logger on a local port (Solarman V5 frames, Modbus
replies, lost replies, exceptions, reboots). `tools/privacy_check.py` looks for private
addresses and the personal strings listed in an untracked `.privacy-local`.

## License

MIT
