#!/usr/bin/env python3
"""Poll a G-Wolves mouse and append successful battery readings to CSV."""

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
import sys
import time

# resolve library path "../../"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gwolves_legacy_ctl import Mouse, ProtocolError

COLUMNS = ('timestamp_utc', 'battery_percent', 'charging')


def append_reading(path: Path, timestamp: str, battery: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+', newline='') as file:
        file.seek(0)
        header = next(csv.reader(file), None)
        if header is not None and tuple(header) != COLUMNS:
            raise ValueError(f'{path} has an unexpected CSV header')
        file.seek(0, 2)
        writer = csv.writer(file)
        if header is None:
            writer.writerow(COLUMNS)
        writer.writerow((timestamp, battery['battery_percent'],
                         int(battery['charging'])))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('battery_log.csv'),
                        help='CSV file to append (default: ./battery_log.csv)')
    parser.add_argument('--interval', type=float, default=60,
                        help='seconds between polls (default: 60)')
    parser.add_argument('--timeout', type=float, default=2,
                        help='seconds to wait for a reply (default: 2)')
    parser.add_argument('--device', help='configuration hidraw node (auto-detected by default)')
    parser.add_argument('--once', action='store_true', help='poll once and exit')
    args = parser.parse_args()
    if args.interval <= 0 or args.timeout <= 0:
        parser.error('--interval and --timeout must be positive')

    offline_reported = False
    try:
        while True:
            try:
                with Mouse(device=args.device, timeout=args.timeout) as mouse:
                    # The receiver answers battery queries even after the mouse
                    # powers off. Check the wireless link on both sides of the
                    # read so cached replies never become new CSV samples.
                    battery = None
                    if mouse.is_connected():
                        candidate = mouse.get_battery()
                        if mouse.is_connected():
                            battery = candidate
            except TimeoutError:
                # A sleeping mouse may time out. Only responses become CSV rows.
                pass
            except (OSError, ProtocolError) as exc:
                print(f'Battery poll failed: {exc}', file=sys.stderr)
                if args.once:
                    return 1
            else:
                timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
                if battery is None:
                    if not offline_reported:
                        print(f'{timestamp}: mouse offline; no battery sample')
                        offline_reported = True
                else:
                    try:
                        append_reading(args.output, timestamp, battery)
                    except (OSError, ValueError) as exc:
                        parser.exit(1, f'Could not write battery CSV: {exc}\n')
                    print(f'{timestamp}: {battery["battery_percent"]}% '
                          f'(charging: {battery["charging"]})')
                    offline_reported = False
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
