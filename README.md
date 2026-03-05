# Protype Printer Components

Custom Moonraker components for Protype 3D printers.

## Components

| Component | Description |
|---|---|
| `bed_surface_calibration` | Autonomous bed surface temperature calibration. Maps heater sensor readings to actual glass surface temperature, accounting for chamber temperature. |

## Installation

```bash
cd ~ && git clone https://github.com/dinamicby/protype-printer-components
cd protype-printer-components && make install
```

If your config directory is non-standard:

```bash
./scripts/install.sh -c /path/to/printer_data/config
```

## Update

```bash
cd ~/protype-printer-components && git pull && make install
```

## Uninstall

```bash
cd ~/protype-printer-components && make uninstall
```

## How it works

The installer creates symlinks from `components/*.py` into Moonraker's internal `~/moonraker/moonraker/components/` directory and adds configuration sections to `moonraker.conf`. This is the same approach used by [moonraker-timelapse](https://github.com/mainsail-crew/moonraker-timelapse).

## Configuration

After installation, the following section is added to `moonraker.conf`:

```ini
[bed_surface_calibration]
stabilize_time: 600
samples_per_point: 10
bed_heater: heater_bed
chamber_heater: Active_Chamber
surface_sensor: bed_glass
tolerance: 2.0
```

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| POST | `/server/calibration/start` | Start calibration with config |
| GET | `/server/calibration/status` | Current state, temperatures, progress |
| POST | `/server/calibration/abort` | Abort running calibration |
| POST | `/server/calibration/skip` | Skip current point |
| GET | `/server/calibration/results?run_id=X` | Full results for a run |
| GET | `/server/calibration/history` | List completed calibrations |
| POST | `/server/calibration/history/delete` | Delete a history entry |

## License

GPLv3
