#!/usr/bin/env python3
"""Plot battery logger CSV readings (requires matplotlib).

Run python plot_battery.py to plot the user service's log, or pass another CSV
as a positional argument. Use --show to also open an interactive plot window.
"""

import argparse
from bisect import bisect_left
import csv
from datetime import datetime
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def read_samples(path: Path) -> list[tuple[datetime, int, bool]]:
    samples = []
    with path.open(newline='') as file:
        reader = csv.DictReader(file)
        columns = {'timestamp_utc', 'battery_percent', 'charging'}
        if not columns.issubset(reader.fieldnames or []):
            raise ValueError('CSV must contain timestamp_utc, battery_percent, charging')
        for row in reader:
            try:
                timestamp = datetime.fromisoformat(row['timestamp_utc'])
                percent = int(row['battery_percent'])
                charging = row['charging']
                if timestamp.tzinfo is None or not 0 <= percent <= 100 or charging not in ('0', '1'):
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ValueError(f'invalid reading at CSV line {reader.line_num}') from exc
            samples.append((timestamp, percent, charging == '1'))
    if not samples:
        raise ValueError('CSV contains no battery readings')
    return sorted(samples, key=lambda sample: sample[0])


def compress_times(samples: list[tuple[datetime, int, bool]], gap_seconds: float
                   ) -> tuple[list[float], list[tuple[float, float, float]]]:
    """Keep sampled time to scale, replacing long gaps with narrow axis breaks."""
    intervals = [(right[0] - left[0]).total_seconds()
                 for left, right in zip(samples, samples[1:])]
    recorded_seconds = sum(interval for interval in intervals if interval <= gap_seconds)
    # Leave a small visible seam even when every sample is isolated.
    seam_seconds = max(recorded_seconds * 0.004, 60)
    positions = [0.0]
    breaks = []
    for interval in intervals:
        left = positions[-1]
        if interval > gap_seconds:
            right = left + min(seam_seconds, interval)
            breaks.append((left, right, interval))
        else:
            right = left + interval
        positions.append(right)
    return positions, breaks


def time_ticks(positions: list[float], count: int = 10) -> list[int]:
    """Choose real sample timestamps spaced across the compressed axis."""
    if positions[-1] == 0:
        return [0]
    indices = []
    for step in range(count):
        target = positions[-1] * step / (count - 1)
        index = min(bisect_left(positions, target), len(positions) - 1)
        if index and target - positions[index - 1] < positions[index] - target:
            index -= 1
        # Sparse logs may have fewer usable labels than the target count.
        if not indices or positions[index] - positions[indices[-1]] >= positions[-1] / (count * 1.5):
            indices.append(index)
    return indices


def main() -> int:
    state_home = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state'))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv', nargs='?', type=Path,
                        default=state_home / 'gwolves-legacy-ctl/battery_log.csv',
                        help='input CSV (default: user service battery log)')
    parser.add_argument('--output', type=Path,
                        default=Path(__file__).resolve().with_name('battery_plot.png'),
                        help='image path (default: battery_plot.png beside this script)')
    parser.add_argument('--timezone', default='UTC', help='display timezone (default: UTC)')
    parser.add_argument('--gap-minutes', type=float, default=5,
                        help='cut longer gaps from the time axis (default: 5 minutes)')
    parser.add_argument('--show', action='store_true', help='also open an interactive window')
    args = parser.parse_args()
    if not math.isfinite(args.gap_minutes) or args.gap_minutes <= 0:
        parser.error('--gap-minutes must be positive and finite')
    try:
        tz = ZoneInfo(args.timezone)
        samples = read_samples(args.csv)
    except (OSError, ValueError, ZoneInfoNotFoundError) as exc:
        parser.exit(1, f'Could not read battery log: {exc}\n')

    try:
        import matplotlib
        if not args.show:
            matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        parser.exit(1, 'Plotting requires matplotlib; install it in your Python environment.\n')

    positions, breaks = compress_times(samples, args.gap_minutes * 60)
    times, levels = [], []
    previous = None
    for position, (timestamp, percent, _) in zip(positions, samples):
        if previous is not None and (timestamp - previous).total_seconds() > args.gap_minutes * 60:
            times.append(position)
            levels.append(float('nan'))
        times.append(position)
        levels.append(percent)
        previous = timestamp

    with plt.rc_context({'font.size': 11, 'axes.spines.top': False,
                         'axes.spines.right': False, 'axes.edgecolor': '#b9c3cf'}):
        fig, ax = plt.subplots(figsize=(13, 5.5), layout='constrained')
        fig.set_facecolor('#f8fafc')
        ax.set_facecolor('#f8fafc')
        ax.step(times, levels, where='post', color='#2563eb', linewidth=1.5)
        for charging, color, label, size in ((False, '#2563eb', 'On battery', 9),
                                             (True, '#ea580c', 'Charging', 32)):
            points = [(position, percent) for position, (_, percent, flag) in zip(positions, samples)
                      if flag == charging]
            if points:
                ax.scatter(*zip(*points), s=size, color=color, label=label, zorder=3)
        ax.set_title('G-Wolves battery history', loc='left', fontsize=18, fontweight='bold', pad=16)
        ax.set_ylabel('Battery (%)')
        ax.set_ylim(-3, 103)
        ax.set_yticks(range(0, 101, 20))
        for index, (left, right, _) in enumerate(breaks):
            ax.axvspan(left, right, color='#dce3eb', alpha=0.65, linewidth=0)
            ax.plot((left + right) / 2, 0, transform=ax.get_xaxis_transform(),
                    marker='D', markersize=4, markerfacecolor='#f8fafc',
                    markeredgecolor='#64748b', linestyle='none', clip_on=False,
                    zorder=5, label='Time gap' if index == 0 else None)
        gap_note = ''
        if breaks:
            skipped_seconds = sum(duration for _, _, duration in breaks)
            gap_note = (f' · gaps > {args.gap_minutes:g} min compressed'
                        f' · {len(breaks)} gaps ({skipped_seconds / 3600:.1f} h unrecorded)')
        ax.set_xlabel(f'Time ({args.timezone}) · {len(samples):,} readings{gap_note}', labelpad=12)
        ticks = time_ticks(positions)
        ax.set_xticks([positions[index] for index in ticks],
                      [samples[index][0].astimezone(tz).strftime('%b %d\n%H:%M')
                       for index in ticks])
        ax.grid(axis='y', color='#dce3eb', linewidth=0.8)
        ax.set_axisbelow(True)
        ax.margins(x=0.02)
        ax.legend(loc='lower right', bbox_to_anchor=(1, 1.01), ncol=3, frameon=False)
        try:
            fig.savefig(args.output, dpi=180)
        except (OSError, ValueError) as exc:
            parser.exit(1, f'Could not save plot: {exc}\n')
        print(f'Saved {args.output.resolve()} ({len(samples):,} readings)')
        if args.show:
            plt.show()
        plt.close(fig)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
