# G-Wolves Legacy Control

## Summary

G-Wolves old mouse software is hard to find and doesn't support Linux at all so I figure I could create a re-implementation in python.

It's vibe reversed and implemented if that's not your thing. I wasn't about to spend countless hours reversing how to set my mouses DPI.

## Supported mice

This isn't targeted at any of G-Wolves new mice which all use WebHID which does work on Linux.

G-Wolves' archived [Classic Wireless software page](https://shop.g-wolves.com/pages/g-wolves-classic-wireless-mouse-firmware-and-software) lists four PAW3370 mice as supported:

- HT-M ACE Wireless
- HT-S ACE Wireless
- HT-S Plus Classic Wireless
- HSK Plus / HSK Plus ACE

## Features

- Automatically discovers compatible mouse configuration interfaces
- Read
  - Battery percentage
  - Charging state
- Reads and configure
  - DPI stages
  - Independent X/Y DPI
  - Stage colors
  - DPI effects
  - Polling rate
  - Lift-off distance
  - Debounce time
  - Sleep timeout
  - Angle snapping
  - Ripple control
  - Lighting
  - Button assignments
  - Keyboard combinations
  - Macros
- Read and selects onboard profiles
- Provides raw EEPROM access, packet tracing, and a dry-run mode for inspection

## Warning

I've only tested with my HSK Plus. If this bricks your mouse it's not my problem! No warranty!

## Usage

```sh
python gwolves_legacy_ctl_cli.py --help
```

The program should auto discover (and select if multiple) supported mice on Linux.

## Library

I split the implementation into a library file and cli file. The library file can be used to write software that interacts with the mouse.

```python
from gwolves_legacy_ctl import Mouse

with Mouse() as mouse:
    battery = mouse.get_battery()
    dpi = battery["battery_percent"] * 50
    mouse.set_dpi(dpi)
```
