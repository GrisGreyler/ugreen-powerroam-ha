# UGREEN PowerRoam for Home Assistant

Home Assistant integration for UGREEN power stations that use the **UGREEN app**
(`com.powerroam.pps`). Fork of [tanka8/ugreen-powerroam](https://github.com/tanka8/ugreen-powerroam)
(MIT), developed against a **PowerRoam 1200W**.

> **Fully vibe-coded fork, provided as is.** The code is written with AI assistance
> and has not been audited. There is no warranty, and no promise of future updates,
> bug fixes or support. Use at your own risk.

> **Tested on one model, one account.** Other PowerRoam models probably share the
> same cloud API and field names, but that is an assumption.

## Transports

Chosen at setup:

| | Bluetooth | Cloud |
|---|---|---|
| Account / internet / WiFi on the unit | not needed | needed |
| Update rate | every ~0.6 s (written to HA at most every 2 s) | bursts of 3 identical frames every ~3.1 s, then a ~15.5 s pause |
| Range | Bluetooth range of the HA host (or a proxy) | anywhere |
| UGREEN app at the same time | **no**: the unit accepts one BLE connection | yes |
| Entity identity | serial number | serial number (same) |

Both entries use the same serial as identity, so you can switch transport and keep
entity history: remove the old entry, add the new one. Two entries for one unit cannot
coexist (deliberate).

The unit has no local IP control path: a full TCP scan of all 65535 ports found nothing
open. It only holds an outbound WiFi connection to `hw-powerapi.ugpps.com`. Local
control is possible only over Bluetooth. Disconnecting the unit from the cloud entirely
means removing WiFi from the unit or blocking it at the router; using the Bluetooth
transport in HA does not do that by itself.

## Entities

| Entity | Type | Notes |
|---|---|---|
| AC Output, DC Output, USB Output, Flashlight | `switch` | both transports |
| Battery | `sensor` | % |
| Battery Health, Cycle Count | `sensor` | diagnostic |
| Battery Capacity Remaining | `sensor` | raw units, uncalibrated |
| Discharge / Charge Time Remaining | `sensor` | native seconds, displayed in hours |
| Total / AC / DC / USB Output Power, Input Power | `sensor` | W |
| Battery Temperature 1/2, Inverter Temperature 1/2 | `sensor` | °C, diagnostic |
| Work Mode | `sensor` | raw numeric mode, meaning unconfirmed |
| Cell 1-7 Voltage | `sensor` | mV, diagnostic |
| Cell Imbalance | `sensor` | mV, diagnostic; max − min of Cell 1-7 Voltage (derived, both transports) |
| AC Input / AC Output / DC Voltage, Fault Code | `sensor` | cloud only, see [Known gaps](#known-gaps) |

Entities are created only for fields the chosen transport can fill (`BLE_SENSORS` in
`const.py` for Bluetooth; everything in `SENSORS` for cloud).

The cloud server reports per-cell voltages too (`cell1_vol` ... `cell7_vol`, plus
`cell_total_vol`); this is not a Bluetooth-only feature.

From these values the integration computes a **Cell Imbalance** sensor: the spread
between the highest and lowest cell voltage in mV. It works over both Bluetooth and
cloud and goes unavailable when fewer than two cell readings are present. A healthy
LiFePO4 pack typically stays within a few mV at rest; the value grows toward the
ends of charge/discharge.

## Install

**Docker (how this fork is run).** Clone the repo and bind-mount the component into the
container read-only. A symlink into `custom_components` does not work inside a
container.

```yaml
services:
  homeassistant:
    image: ghcr.io/home-assistant/home-assistant:stable
    network_mode: host
    volumes:
      - ./config:/config
      - /path/to/ugreen-powerroam/custom_components/ugreen_powerroam:/config/custom_components/ugreen_powerroam:ro
```

Update:

```bash
cd /path/to/ugreen-powerroam && git pull
docker restart homeassistant
```

**HACS** - Custom repositories, add this repository as an *Integration*, install,
restart.

Then **Settings, Devices & services, Add integration, UGREEN PowerRoam**, pick a
transport. For Bluetooth the HA `bluetooth` integration and an adapter within range of
the unit are required (in Docker, HA needs access to the host's Bluetooth stack), and
the UGREEN phone app must be closed. For cloud, sign in with the UGREEN app account.

## How it works

### Cloud

- **Auth** - `GET /app/v1/sa/encrypt/key` returns an RSA public key and `uuid`. Email
  and password are RSA/PKCS1v1.5-encrypted and posted to `POST /app/v1/login`, which
  returns a `token` sent as a header on later requests.
- **Device list** - `GET /app/v1/device/list`; the first device's `deviceModelName` is
  its id everywhere.
- **Control** - `POST /app/v1/device/setDeviceInfo` with
  `{"deviceName": ..., "map": {"switch_ac": 1}}`. Every switch is this call with a
  different key.
- **Telemetry** - WebSocket
  `wss://hw-powerapi.ugpps.com:8089/app/device/websocket/{userId}/{deviceName}`,
  same `token` header. Sending `{"userId": ..., "content": "ugreenSocketConnection"}`
  subscribes and, repeated every 25 s, keeps the connection alive. The server pushes
  one flat JSON object with every field the device reports.
- **Recovery** - no telemetry for 90 s means the socket is treated as dead and
  reconnected (backoff 5 to 60 s). After 3 consecutive failures the client logs in
  again for a fresh token. Entities go unavailable while the socket is down.

### Bluetooth

Wire format is in `protocol.py` (pure Python, unit tested against frames captured from
a real unit):

```
5A A5 | A1 C0 | cmd | len (uint16 LE) | data | crc (uint16 LE)
```

CRC is Modbus CRC-16; device replies carry the direction bytes swapped (`C0 A1`). GATT
service `ABF0`: write to `ABF1`, subscribe to `ABF2`. The service is not advertised,
so discovery matches the local name (`ugreen*`, observed `ugreen gs1200`). No pairing,
bonding or encryption.

**Trap:** the `0x16` switch opcode has two different layouts. The device reports 12
fields, a write takes 11 (no `lowBatteryWarning`). Echoing a received payload back as a
write shifts every field after the first by one position; this once silently switched
battery preserving mode off. `protocol.py` keeps the layouts apart and refuses to build
a write from a partial state. `0x16` replaces the whole state, so a write needs the
current values of all eleven fields.

## Cloud behaviour worth knowing

**Update cadence.** Measured on one unit over ~15 minutes (199 frames, 2026-09-29):
every ~3.1 s a burst of 3 identical frames (1-3 ms apart); after six bursts a pause of
~15.5 s, so one cycle is ~33 s. Average gap 1.7 s, median 0 s, longest 18.6 s. One
unit, one short sample; UGREEN can change this.

**Flapping values.** The cloud occasionally sends a frame where `usb_sw` (and also
`work_mode`) briefly takes another value for a second or two with no physical change,
while the other switches stay unchanged. Cause unknown; the frame contains every key,
so it is not a missing field.

**Filter in `api.py`.** For `usb_sw`, `work_mode`, `switch_ac`, `switch_dc` and
`lamp_sw`, a frame in which one of these differs from the current state is dropped
entirely until the same new value has been seen again at least 15 s after it first
appeared. Consequences:

- Toggles made from HA update the state optimistically, so they are not delayed; stale
  frames still carrying the old value are held back instead of flipping the switch
  back.
- A change made on the unit itself shows up in HA only after this confirmation, i.e.
  roughly 15 to 33 s later.
- Because whole frames are dropped, other values in a held frame are delayed too.

Debug logging: enable `custom_components.ugreen_powerroam` in `logger:` to log frame
timing, `usb_sw` / `work_mode` / switch transitions and raw frames.

## Known gaps

- **Cell voltages over cloud.** Entities `cell_voltage_1..7` are created on both
  transports, but the cloud frame names the fields `cell{n}_vol` while BLE decoding
  produces `cell_voltage_{n}`. Check that the cloud path maps them in your build;
  `const.py` still labels these sensors as BLE-only.
- **AC/DC voltage sensors and Fault Code.** Upstream measurements on a 1200W gave
  `0.0` / empty for `ac_in_vol`, `dc_vol` and `device_fault2` on both transports
  (`ac_vol` is present in the sample frame). Not re-verified in this fork. Do not read
  `0.0` as "0 V", read it as "the device did not report".
- **U-Turbo** (`switch_conpower`) is not implemented: the write key was never
  confirmed by capture.
- **Settings are visible in telemetry but not controllable:** `bat_health_set`,
  `low_sound_set`, `bee_sound_set_key`, `bee_sound_set_warning`, `display_bright_set`,
  `low_power_al_set`, `time_shutdown`, `time_dis_shutdown`, `timeoff_set`,
  `timeoff_cap`, `timeoff_zoom`, `ac_freq_set`, `car_charge_i_set`. Each needs its own
  traffic capture to confirm write semantics. The app also has a parallel BLE
  transport, so they are likely reachable over Bluetooth too (unverified).
- **Reported but not exposed:** `cell_total_vol`, `usb1_vol`, `usb2_vol`, `type_c1_vol`,
  `type_c2_vol`, `low_battery`, `self_check`, `switch_lock`, `switch_all`, firmware
  version fields.
- **Firmware update** is not supported.
- **Raw values:** `bat_cap_remain` and `work_mode` scales and meanings are unconfirmed.
- **No reauth flow:** the socket re-logs in by itself after failures, but wrong stored
  credentials (changed password) are not surfaced in the UI.
- **Single device per account:** only the first device from `device/list` is used.
- Error messages are hardcoded English.

## Development

- Issues in this repository can be picked up by a GitHub Actions workflow
  (`.github/workflows/gemini-coder.yml`): a new issue, or the `gemini` label, runs
  Aider with Gemini on the issue text and commits the result straight to `main`.
  Review the diff after it runs.
- Tests: `pip install pytest aiohttp cryptography && python -m pytest -v` (BLE codec,
  frame parser, RSA login; no network or hardware). `tests_ha/` runs the config flow
  inside Home Assistant (Linux/macOS only, CI runs it). CI also runs `ruff`, `hassfest`
  and HACS validation.
- Checking another model over Bluetooth (read-only): `pip install bleak &&
  python scripts/hardware_check.py`. `--flashlight` blinks the light to test the write
  path. If the switch block length differs from the 1200W's, do not write switches.
- Adding a setting: capture the app's traffic (e.g. mitmproxy with a system-trusted
  CA), toggle the control, note the `map` key and value, add it to `const.py`.

## Credits and licence

Based on tanka8/ugreen-powerroam. Reverse engineering, the integration and its tests
were largely written by Claude (Anthropic) under the maintainer's direction and
tested on the maintainer's own unit. Not affiliated with or endorsed by UGREEN; the
API is undocumented and can change at any time. No support is promised.

MIT.
