#!/usr/bin/env python3
"""Control legacy G-Wolves PAW3370 wireless mice."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import gwolves_legacy_ctl as ctl


def number(value: str) -> int:
    return int(value, 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-d', '--device', help='configuration hidraw node (auto-detected by default)')
    parser.add_argument('-t', '--timeout', type=float, default=2.0)
    parser.add_argument('-v', '--verbose', action='store_true', help='print vendor USB packets')
    parser.add_argument('-n', '--dry-run', action='store_true',
                        help='show set packets without opening any device')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='list compatible mouse configuration interfaces')

    read = commands.add_parser('read-eeprom', help='read a raw EEPROM range')
    read.add_argument('address', type=number)
    read.add_argument('length', type=number)
    write = commands.add_parser('write-eeprom', help='write exact bytes including checksums')
    write.add_argument('address', type=number)
    write.add_argument('hex_data', help='hex bytes, quoted if separated by spaces')

    get = commands.add_parser('get', help='read settings')
    getters = get.add_subparsers(dest='field', required=True)
    for field in ('all', 'info', 'battery', 'profile', 'rate', 'stage', 'stage-count',
                  'stages', 'lod',
                  'debounce', 'sleep', 'angle-snapping', 'ripple-control',
                  'light-off-moving', 'dpi-effect', 'lighting'):
        getters.add_parser(field)
    dpi = getters.add_parser('dpi')
    dpi.add_argument('stage', type=int, nargs='?', choices=range(1, 9))
    color = getters.add_parser('dpi-color')
    color.add_argument('stage', type=int, choices=range(1, 9))
    for field in ('button', 'combo', 'macro'):
        item = getters.add_parser(field)
        item.add_argument('slot', type=int, choices=range(1, 17))

    set_command = commands.add_parser('set', help='change one setting')
    setters = set_command.add_subparsers(dest='field', required=True)

    dpi = setters.add_parser('dpi', help='set DPI in the active or selected stage')
    dpi.add_argument('x', type=int)
    dpi.add_argument('--y', type=int, help='separate Y DPI; defaults to X DPI')
    dpi.add_argument('--stage', type=int, choices=range(1, 9))
    stages = setters.add_parser('stages', help='replace the enabled DPI stages')
    stages.add_argument('values', nargs='+', type=int)
    stages.add_argument('--active', type=int, default=1)

    def value_parser(field: str, **kwargs):
        item = setters.add_parser(field)
        item.add_argument('value', type=int, **kwargs)

    value_parser('stage', choices=range(1, 9))
    value_parser('lod', choices=(1, 2))
    value_parser('rate', choices=tuple(ctl.RATES))
    value_parser('debounce')
    value_parser('sleep')
    for field in ctl.FLAGS:
        item = setters.add_parser(field)
        item.add_argument('value', choices=('on', 'off'))

    color = setters.add_parser('dpi-color')
    color.add_argument('stage', type=int, choices=range(1, 9))
    color.add_argument('rgb', nargs=3, type=int, metavar='RGB')
    effect = setters.add_parser('dpi-effect')
    effect.add_argument('mode', type=number)
    effect.add_argument('--brightness', type=int, default=5)
    effect.add_argument('--speed', type=int, default=5)
    lighting = setters.add_parser('lighting')
    lighting.add_argument('mode', choices=tuple(ctl.LED_MODES))
    lighting.add_argument('--rgb', nargs=3, type=int, default=(255, 0, 255))
    lighting.add_argument('--brightness', type=int, default=5)
    lighting.add_argument('--speed', type=int, default=5)
    button = setters.add_parser('button')
    button.add_argument('slot', type=int, choices=range(1, 17))
    button.add_argument('function', help='named action or raw 24-bit code, e.g. 0x0102')
    combo = setters.add_parser('combo')
    combo.add_argument('slot', type=int, choices=range(1, 17))
    combo.add_argument('usages', nargs='*', type=number)
    combo.add_argument('--modifiers', type=number, default=0,
                       help='bit mask: Ctrl=1 Shift=2 Alt=4 GUI=8')
    macro = setters.add_parser('macro')
    macro.add_argument('slot', type=int, choices=range(1, 17))
    macro.add_argument('file', type=Path)
    profile = setters.add_parser('profile')
    profile.add_argument('index', type=int)
    return parser


def get_value(device: ctl.Mouse, args: argparse.Namespace):
    field = args.field
    if field == 'all':
        return device.get_settings()
    if field in ctl.FLAGS:
        return {field.replace('-', '_'): device.get_flag(field)}
    if field == 'dpi':
        return device.get_dpi(args.stage)
    if field == 'dpi-color':
        return {'stage': args.stage, 'rgb': device.get_dpi_color(args.stage)}
    if field in ('button', 'combo', 'macro'):
        return getattr(device, 'get_' + field)(args.slot)
    method = getattr(device, 'get_' + field.replace('-', '_'))
    value = method()
    if isinstance(value, dict):
        return value
    return {field.replace('-', '_'): value}


def set_value(device: ctl.Mouse, args: argparse.Namespace):
    field = args.field
    if field in ctl.FLAGS:
        device.set_flag(field, args.value == 'on')
    elif field == 'dpi':
        device.set_dpi(args.x, args.y, args.stage)
    elif field == 'stages':
        device.set_stages(args.values, args.active)
    elif field in ('stage', 'lod', 'rate', 'debounce', 'sleep'):
        getattr(device, 'set_' + field)(args.value)
    elif field == 'dpi-color':
        device.set_dpi_color(args.stage, args.rgb)
    elif field == 'dpi-effect':
        device.set_dpi_effect(args.mode, args.brightness, args.speed)
    elif field == 'lighting':
        device.set_lighting(args.mode, args.rgb, args.brightness, args.speed)
    elif field == 'button':
        function = (ctl.BUTTONS[args.function] if args.function in ctl.BUTTONS
                    else number(args.function))
        device.set_button(args.slot, function)
    elif field == 'combo':
        device.set_combo(args.slot, args.usages, args.modifiers)
    elif field == 'macro':
        document = json.loads(args.file.read_text())
        device.set_macro(args.slot, document['name'], document['events'],
                         document.get('repeat', 1))
    elif field == 'profile':
        device.set_profile(args.index)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == 'list':
            result = ctl.discover()
        else:
            with ctl.Mouse(args.device, args.timeout, args.verbose, args.dry_run) as device:
                if args.command == 'read-eeprom':
                    result = {'address': args.address,
                              'hex': device.read_eeprom(args.address, args.length).hex(' ')}
                elif args.command == 'write-eeprom':
                    device.write_eeprom(args.address, bytes.fromhex(args.hex_data))
                    result = None
                elif args.command == 'get':
                    result = get_value(device, args)
                else:
                    set_value(device, args)
                    result = None
                if args.dry_run:
                    result = {'dry_run': True, 'packets': device.planned_packets}
                elif result is None:
                    result = {'acknowledged': True,
                              'action': getattr(args, 'field', args.command)}
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ctl.ProtocolError, TimeoutError, ValueError, KeyError,
            TypeError, json.JSONDecodeError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
