"""Linux controller library for legacy G-Wolves PAW3370 wireless mice."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
import select
import sys
import time

VID = 0x33E4
PIDS = (0x001A, 0x00AA)
REPORT_SIZE = 17
SET_FEATURE = (3 << 30) | (REPORT_SIZE << 16) | (ord('H') << 8) | 6
RATES = {125: 8, 250: 4, 500: 2, 1000: 1}
DPI_BRIGHTNESS = (16, 30, 60, 90, 128, 150, 180, 210, 230, 255)
LED_SPEED = (255, 230, 210, 190, 170, 150, 130, 110, 70, 40)
LED_BRIGHTNESS = (45, 50, 75, 100, 125, 150, 175, 200, 225, 255)
LED_MODES = {'off': 4, 'steady': 2, 'breathing': 1, 'streaming': 0,
             'neon': 3, 'single-color-flow': 5, 'colorful-breathing': 7}
BUTTONS = {'disabled': 0, 'left': 0x0101, 'right': 0x0201,
           'middle': 0x0401, 'back': 0x0801, 'forward': 0x1001,
           'double-click': 0x023204, 'triple-click': 0x033204,
           'dpi-cycle': 0x0102, 'dpi-up': 0x0202, 'dpi-down': 0x0302,
           'rate-cycle': 8, 'led-toggle': 7, 'led-cycle': 9}
FLAGS = {'angle-snapping': 0xAF, 'ripple-control': 0xB1,
         'light-off-moving': 0xB3}


class ProtocolError(RuntimeError):
    """The device returned an unsuccessful or malformed response."""


def bounded(value: int, low: int, high: int, name: str) -> int:
    if not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in {low}..{high}')
    return value


def checked(data: bytes) -> bytes:
    """Append the checksum used by each EEPROM record (separate from USB CRC)."""
    return data + bytes(((0x55 - sum(data)) & 255,))


def check_record(data: bytes, name: str) -> bytes:
    if not data or sum(data) & 255 != 0x55:
        raise ProtocolError(f'Invalid {name} record checksum: {data.hex(" ")}')
    return data[:-1]


def encode_dpi(x: int, y: int | None = None) -> bytes:
    """PAW3370 encoding, limited to ranges in reversing/Cfg.ini."""
    def axis(dpi):
        bounded(dpi, 50, 19000, 'DPI')
        step, multiplier = (50, 0) if dpi <= 10000 else (100, 2)
        if dpi % step:
            raise ValueError(f'DPI {dpi} must be a multiple of {step}')
        return dpi // step - 1, multiplier
    xraw, xmul = axis(x)
    yraw, ymul = axis(x if y is None else y)
    return checked(bytes((xraw, yraw, xmul | (ymul << 4))))


def decode_dpi(data: bytes) -> tuple[int, int]:
    x, y, multiplier = check_record(data, 'DPI')
    def axis(raw, flags):
        return (raw + 1) * (100 if flags & 2 else 50) * (2 if flags & 1 else 1)
    return axis(x, multiplier & 15), axis(y, multiplier >> 4)


def rgb_bytes(rgb) -> bytes:
    if len(rgb) != 3:
        raise ValueError('RGB needs three components')
    return bytes(bounded(v, 0, 255, 'RGB component') for v in rgb)


def _table_index(value: int, table: tuple[int, ...]) -> int | None:
    """Return a public one-based level for an encoded table value."""
    try:
        return table.index(value) + 1
    except ValueError:
        return None


def decode_button(data: bytes, slot: int | None = None) -> dict:
    code = int.from_bytes(check_record(data, 'button'), 'little')
    result = {'code': code, 'code_hex': f'0x{code:06x}',
              'action': next((name for name, value in BUTTONS.items()
                              if value == code), None)}
    if slot is not None:
        result['slot'] = slot
    return result


def decode_dpi_effect(data: bytes) -> dict:
    if len(data) != 6:
        raise ProtocolError(f'DPI effect record must be 6 bytes, got {len(data)}')
    mode = check_record(data[0:2], 'DPI effect mode')[0]
    brightness_raw = check_record(data[2:4], 'DPI effect brightness')[0]
    speed_raw = check_record(data[4:6], 'DPI effect speed')[0]
    return {'mode': mode,
            'brightness': _table_index(brightness_raw, DPI_BRIGHTNESS),
            'speed': speed_raw if 1 <= speed_raw <= 10 else None,
            'brightness_raw': brightness_raw, 'speed_raw': speed_raw}


def decode_lighting(data: bytes) -> dict:
    mode_raw, red, green, blue, speed_raw, brightness_raw = check_record(
        data, 'lighting')
    mode = next((name for name, value in LED_MODES.items() if value == mode_raw), None)
    if mode == 'streaming':
        speed = 11 - speed_raw if 1 <= speed_raw <= 10 else None
        brightness = ((brightness_raw - 52) // 20
                      if brightness_raw in range(72, 253, 20) else None)
    else:
        speed = _table_index(speed_raw, LED_SPEED)
        brightness = _table_index(brightness_raw, LED_BRIGHTNESS)
    return {'mode': mode, 'mode_raw': mode_raw, 'rgb': [red, green, blue],
            'brightness': brightness, 'speed': speed,
            'brightness_raw': brightness_raw, 'speed_raw': speed_raw}


def decode_combo(data: bytes) -> list[dict]:
    if not data:
        raise ProtocolError('Empty combination record')
    count = data[0]
    length = 2 + 3 * count
    if count > 10 or length > len(data):
        raise ProtocolError(f'Invalid combination event count {count}')
    body = check_record(data[:length], 'combination')
    kinds = {0: 'modifier', 1: 'key', 2: 'consumer'}
    events = []
    for offset in range(1, len(body), 3):
        flag, code, reserved = body[offset:offset + 3]
        kind = kinds.get(flag & 0x0F)
        direction = flag & 0xC0
        if kind is None or direction not in (0x40, 0x80) or reserved:
            raise ProtocolError(f'Invalid combination event: {body[offset:offset + 3].hex(" ")}')
        events.append({'type': kind, 'code': code, 'down': direction == 0x80})
    return events


def decode_macro(data: bytes) -> dict:
    if len(data) < 33:
        raise ProtocolError(f'Macro record is too short: {len(data)} bytes')
    name_length = data[0]
    if name_length > 30 or name_length % 2:
        raise ProtocolError(f'Invalid macro name length {name_length}')
    try:
        name = data[1:1 + name_length].decode('utf-16-le')
    except UnicodeDecodeError as exc:
        raise ProtocolError('Invalid macro UTF-16 name') from exc
    count = data[31]
    end = 32 + 5 * count
    if count > 70 or end >= len(data):
        raise ProtocolError(f'Invalid macro event count {count}')
    check_record(data[31:end + 1], 'macro events')
    kinds = {0: 'modifier', 1: 'key', 4: 'mouse'}
    events = []
    for offset in range(32, end, 5):
        flag, code, reserved, delay_hi, delay_lo = data[offset:offset + 5]
        kind = kinds.get(flag & 0x0F)
        direction = flag & 0xC0
        if kind is None or direction not in (0x40, 0x80) or reserved:
            raise ProtocolError(f'Invalid macro event: {data[offset:offset + 5].hex(" ")}')
        events.append({'type': kind, 'code': code, 'down': direction == 0x80,
                       'delay_ms': (delay_hi << 8) | delay_lo})
    return {'name': name, 'events': events}


def encode_macro(name: str, events: list[dict]) -> bytes:
    """Build a macro slot payload. Events use HID usages, not Windows key codes.

    Each event: {"type": "key"|"modifier"|"mouse",
                 "code": int, "down": bool, "delay_ms": int}.
    """
    name_bytes = name.encode('utf-16-le')
    if b'\0\0' == name_bytes[:2] or '\0' in name or len(name_bytes) > 30:
        raise ValueError('macro name must contain at most 15 UTF-16 code units, without NUL')
    if not 1 <= len(events) <= 70:
        raise ValueError('a macro must contain 1..70 events')
    slot = bytearray(0x180)
    slot[0] = len(name_bytes)
    slot[1:1 + len(name_bytes)] = name_bytes
    slot[31] = len(events)
    for index, event in enumerate(events):
        delay = bounded(event.get('delay_ms', 0), 0, 65535, 'delay_ms')
        kind = event['type']
        types = {'modifier': 0, 'key': 1, 'mouse': 4}
        if kind not in types or not isinstance(event.get('down'), bool):
            raise ValueError('event needs type key/modifier/mouse and boolean down')
        code = bounded(event['code'], 1, 255, 'HID code/mask')
        flag = (0x80 if event['down'] else 0x40) | types[kind]
        start = 32 + 5 * index
        slot[start:start + 5] = bytes((flag, code, 0, delay >> 8, delay & 255))
    end = 32 + 5 * len(events)
    # OemDrv computes this BEFORE filling the name. Only count + events enter it.
    slot[end] = (0x55 - sum(slot[31:end])) & 255
    used = end + 1
    if used <= 0x17D:
        used += 3  # the original app adds three zero terminator bytes when they fit
    return bytes(slot[:used])


def discover() -> list[dict]:
    """Match both USB IDs and the vendor configuration report descriptor."""
    devices = []
    for node in sorted(Path('/sys/class/hidraw').glob('hidraw*')):
        try:
            info = dict(line.split('=', 1) for line in
                        (node / 'device/uevent').read_text().splitlines() if '=' in line)
            bus, vendor, product = (int(v, 16) for v in info['HID_ID'].split(':'))
            if bus != 3 or vendor != VID or product not in PIDS:
                continue
            descriptor = (node / 'device/report_descriptor').read_bytes()
            # This receiver's configuration collection is vendor page FF02,
            # report 08 (16 feature bytes), with input report 09 on the same node.
            if b'\x06\x02\xff' not in descriptor or b'\x85\x08' not in descriptor:
                continue
            if b'\x85\x09' not in descriptor:
                continue
            devices.append({'path': '/dev/' + node.name,
                            'vid': f'{vendor:04x}', 'pid': f'{product:04x}',
                            'name': info.get('HID_NAME', ''),
                            'physical': info.get('HID_PHYS', '')})
        except (OSError, KeyError, ValueError):
            continue
    return devices


def packet(command: int, address: int = 0, data: bytes = b'',
           read_length: int | None = None) -> bytes:
    """08 cmd status addr_hi addr_lo length data[10] checksum."""
    if not 0 <= command <= 255 or not 0 <= address <= 0xFFFF:
        raise ValueError('command/address out of range')
    if len(data) > 10 or (read_length is not None and not 0 <= read_length <= 10):
        raise ValueError('at most 10 data bytes per command')
    if data and read_length is not None:
        raise ValueError('read_length and data are mutually exclusive')
    p = bytearray(REPORT_SIZE)
    p[:6] = bytes((8, command, 0, address >> 8, address & 255,
                   len(data) if read_length is None else read_length))
    p[6:6 + len(data)] = data
    p[-1] = (0x55 - sum(p)) & 255
    return bytes(p)


class Mouse:
    def __init__(self, device: str | None = None, timeout: float = 2.0,
                 verbose: bool = False, dry_run: bool = False):
        self.dry_run = dry_run
        self.planned_packets = []
        self.verbose = verbose
        self.fd = None
        if dry_run:
            self.path = device
            return
        devices = discover()
        if device is None:
            if len(devices) != 1:
                raise ProtocolError(f'Expected one matching mouse interface, found {devices}; '
                                    'use list or --device /dev/hidrawN')
            device = devices[0]['path']
        if device not in {d['path'] for d in devices}:
            raise ProtocolError(f'{device} does not match the mouse configuration interface')
        if timeout <= 0:
            raise ValueError('timeout must be positive')
        self.path = device
        self.timeout = timeout
        self.verbose = verbose
        self.fd = os.open(device, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)

    def _trace(self, direction: str, data: bytes):
        if self.verbose:
            print(f'{direction} {data.hex(" ")}', file=sys.stderr)

    def command(self, command: int, address: int = 0, data: bytes = b'',
                read_length: int | None = None,
                allow_nonzero_status: bool = False) -> bytes:
        p = packet(command, address, data, read_length)
        if self.dry_run:
            if command not in (2, 7, 0x10):
                raise ValueError('dry-run cannot read hardware; for dpi specify --stage')
            self.planned_packets.append(p.hex(' '))
            return b''
        # Discard stale responses without claiming or detaching the input driver.
        deadline = time.monotonic() + 0.1
        while time.monotonic() < deadline:
            try:
                os.read(self.fd, 64)
            except BlockingIOError:
                break
        self._trace('TX', p)
        fcntl.ioctl(self.fd, SET_FEATURE, p)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                raise TimeoutError(f'Command 0x{command:02x} timed-out. If wireless the mouse may be asleep. Try move it')
            try:
                reply = os.read(self.fd, 64)
            except BlockingIOError:
                continue
            if not reply:
                raise ProtocolError('Mouse disconnected')
            # The same interface carries keyboard/media input. Never interpret it
            # as a configuration response or print it in a diagnostic trace.
            if reply[0] != 9:
                continue
            self._trace('RX', reply)
            if len(reply) != REPORT_SIZE:
                raise ProtocolError(f'Unexpected vendor report length {len(reply)}')
            if sum(reply) & 255 != 0x55:
                raise ProtocolError('Invalid vendor response checksum')
            if reply[1] != command:
                continue  # includes asynchronous command 0A notifications
            if reply[2] and not allow_nonzero_status:
                raise ProtocolError(f'Command 0x{command:02x}: status 0x{reply[2]:02x}')
            if reply[5] > 10:
                raise ProtocolError('Response data length exceeds 10 bytes')
            if read_length is not None and reply[5] != read_length:
                raise ProtocolError(f'Expected {read_length} response bytes, got {reply[5]}')
            if command == 8 and reply[3:5] != p[3:5]:
                raise ProtocolError('EEPROM reply address does not match request')
            return reply

    def battery(self) -> dict:
        reply = self.command(0x04)
        if reply[5] != 2:
            raise ProtocolError('Battery response must contain two data bytes')
        percent, charging = reply[6:8]
        if percent > 100:
            raise ProtocolError(f'Invalid battery percentage {percent}: {reply.hex(" ")}')
        return {'device': self.path, 'battery_percent': percent,
                'charging': bool(charging), 'charging_raw': charging,
                'response_hex': reply.hex(' ')}

    def get_battery(self) -> dict:
        """Return battery telemetry; alias retained as battery() for compatibility."""
        return self.battery()

    def is_connected(self) -> bool:
        """Query whether the wireless mouse is online (the receiver can remain plugged in)."""
        # The receiver can report offline as either a zero data byte or a
        # nonzero command status. The vendor application treats both as offline.
        reply = self.command(0x03, allow_nonzero_status=True)
        if reply[2]:
            return False
        if reply[5] != 1:
            raise ProtocolError('Connection response must contain one data byte')
        state = reply[6]
        if state not in (0, 1):
            raise ProtocolError(f'Invalid connection state {state}: {reply.hex(" ")}')
        return bool(state)

    def read_eeprom(self, address: int, length: int) -> bytes:
        bounded(address, 0, 0xFFFF, 'address')
        bounded(length, 1, 0x10000 - address, 'length')
        result = bytearray()
        while len(result) < length:
            size = min(10, length - len(result))
            reply = self.command(8, address + len(result), read_length=size)
            result.extend(reply[6:6 + size])
            time.sleep(0.01)
        return bytes(result)

    def write_eeprom(self, address: int, data: bytes, chunk_size: int = 10):
        bounded(address, 0, 0xFFFF, 'address')
        bounded(len(data), 1, 0x10000 - address, 'data length')
        bounded(chunk_size, 1, 10, 'chunk size')
        for offset in range(0, len(data), chunk_size):
            self.command(7, address + offset, data[offset:offset + chunk_size])
            if not self.dry_run:
                time.sleep(0.01)

    def _set_byte(self, address: int, value: int):
        self.write_eeprom(address, checked(bytes((bounded(value, 0, 255, 'value'),))))

    def _get_byte(self, address: int) -> int:
        return check_record(self.read_eeprom(address, 2), f'0x{address:04x}')[0]

    def get_dpi(self, stage: int | None = None) -> dict:
        if stage is None:
            stage = self.get_stage()
        bounded(stage, 1, 8, 'DPI stage')
        x, y = decode_dpi(self.read_eeprom(0x0C + 4 * (stage - 1), 4))
        return {'stage': stage, 'x': x, 'y': y}

    def set_dpi(self, x: int, y: int | None = None, stage: int | None = None):
        record = encode_dpi(x, y)  # validate before any I/O
        if stage is None:
            stage = self._get_byte(4) + 1
        bounded(stage, 1, 8, 'DPI stage')
        self.write_eeprom(0x0C + 4 * (stage - 1), record)

    def set_stage(self, stage: int):
        self._set_byte(4, bounded(stage, 1, 8, 'DPI stage') - 1)

    def get_stage(self) -> int:
        stage = self._get_byte(4) + 1
        return bounded(stage, 1, 8, 'DPI stage')

    def get_stage_count(self) -> int:
        return bounded(self._get_byte(2), 1, 8, 'number of DPI stages')

    def get_stages(self) -> dict:
        count = self.get_stage_count()
        return {'active_stage': self.get_stage(),
                'stages': [self.get_dpi(stage) | {'rgb': self.get_dpi_color(stage)}
                           for stage in range(1, count + 1)]}

    def set_stages(self, values: list[int], active: int = 1):
        bounded(len(values), 1, 8, 'number of DPI stages')
        bounded(active, 1, len(values), 'active stage')
        records = b''.join(encode_dpi(dpi) for dpi in values)
        self.write_eeprom(0x0C, records, chunk_size=8)
        self._set_byte(2, len(values))
        self.set_stage(active)

    def set_lod(self, millimeters: int):
        self._set_byte(0x0A, bounded(millimeters, 1, 2, 'LOD in mm'))

    def get_lod(self) -> int:
        return bounded(self._get_byte(0x0A), 1, 2, 'LOD in mm')

    def set_rate(self, hz: int):
        if hz not in RATES:
            raise ValueError('polling rate must be 125, 250, 500, or 1000 Hz')
        self._set_byte(0, RATES[hz])

    def get_rate(self) -> int:
        raw = self._get_byte(0)
        try:
            return next(hz for hz, encoded in RATES.items() if encoded == raw)
        except StopIteration as exc:
            raise ProtocolError(f'Unknown polling-rate byte {raw}') from exc

    def set_debounce(self, milliseconds: int):
        self._set_byte(0xA9, bounded(milliseconds, 0, 255, 'debounce in ms'))

    def get_debounce(self) -> int:
        return self._get_byte(0xA9)

    def set_sleep(self, seconds: int):
        bounded(seconds, 10, 2550, 'sleep interval in seconds')
        if seconds % 10:
            raise ValueError('sleep interval must be a multiple of 10 seconds')
        self._set_byte(0xAD, seconds // 10)

    def get_sleep(self) -> int:
        return self._get_byte(0xAD) * 10

    def set_flag(self, name: str, enabled: bool):
        if name not in FLAGS or not isinstance(enabled, bool):
            raise ValueError('invalid flag or boolean value')
        self._set_byte(FLAGS[name], int(enabled))

    def get_flag(self, name: str) -> bool:
        if name not in FLAGS:
            raise ValueError(f'unknown flag: {name}')
        raw = self._get_byte(FLAGS[name])
        if raw not in (0, 1):
            raise ProtocolError(f'Invalid {name} value {raw}')
        return bool(raw)

    def set_dpi_color(self, stage: int, rgb):
        bounded(stage, 1, 8, 'DPI stage')
        self.write_eeprom(0x2C + 4 * (stage - 1), checked(rgb_bytes(rgb)))

    def get_dpi_color(self, stage: int) -> list[int]:
        bounded(stage, 1, 8, 'DPI stage')
        return list(check_record(self.read_eeprom(0x2C + 4 * (stage - 1), 4),
                                 'DPI color'))

    def set_dpi_effect(self, mode: int, brightness: int = 5, speed: int = 5):
        bounded(mode, 0, 255, 'effect mode byte')
        bounded(brightness, 1, 10, 'brightness')
        bounded(speed, 1, 10, 'speed')
        self.write_eeprom(0x4C, checked(bytes((mode,))) +
                          checked(bytes((DPI_BRIGHTNESS[brightness - 1],))) +
                          checked(bytes((speed,))))

    def get_dpi_effect(self) -> dict:
        return decode_dpi_effect(self.read_eeprom(0x4C, 6))

    def set_lighting(self, mode: str, rgb=(255, 0, 255), brightness: int = 5,
                     speed: int = 5):
        if mode not in LED_MODES:
            raise ValueError(f'unknown lighting mode: {mode}')
        color = rgb_bytes(rgb)
        bounded(brightness, 1, 10, 'brightness')
        bounded(speed, 1, 10, 'speed')
        if mode == 'streaming':
            speed_byte = 11 - speed
            brightness_byte = 52 + brightness * 20
        else:
            speed_byte = LED_SPEED[speed - 1]
            brightness_byte = LED_BRIGHTNESS[brightness - 1]
        self.write_eeprom(0xA0, checked(bytes((LED_MODES[mode],)) + color +
                                     bytes((speed_byte, brightness_byte))))

    def get_lighting(self) -> dict:
        return decode_lighting(self.read_eeprom(0xA0, 7))

    def set_button(self, button: int, action: str | int):
        """Button numbers are hardware matrix slots (1-based), per reversing/Cfg.ini K*_1."""
        bounded(button, 1, 16, 'button matrix slot')
        code = BUTTONS[action] if isinstance(action, str) else action
        bounded(code, 0, 0xFFFFFF, 'button function code')
        self.write_eeprom(0x60 + 4 * (button - 1), checked(code.to_bytes(3, 'little')))

    def get_button(self, button: int) -> dict:
        bounded(button, 1, 16, 'button matrix slot')
        return decode_button(self.read_eeprom(0x60 + 4 * (button - 1), 4), button)

    def set_macro(self, button: int, name: str, events: list[dict],
                  repeat: int | str = 1):
        bounded(button, 1, 16, 'button matrix slot')
        if isinstance(repeat, str):
            mode = {'while-held': 0xFF, 'until-press': 0xFE}[repeat]
        else:
            mode = bounded(repeat, 1, 253, 'macro repeat')
        slot = encode_macro(name, events)
        index = button - 1
        self.write_eeprom(0x300 + index * 0x180, slot)
        self.set_button(button, 6 | (index << 8) | (mode << 16))

    def get_macro(self, button: int) -> dict:
        assignment = self.get_button(button)
        code = assignment['code']
        if code & 0xFF != 6:
            raise ProtocolError(f'Button slot {button} is not assigned to a macro')
        index = (code >> 8) & 0xFF
        if index >= 16:
            raise ProtocolError(f'Invalid macro slot index {index}')
        result = decode_macro(self.read_eeprom(0x300 + index * 0x180, 0x180))
        repeat_raw = (code >> 16) & 0xFF
        repeat = {0xFF: 'while-held', 0xFE: 'until-press'}.get(repeat_raw, repeat_raw)
        return {'button': button, 'macro_slot': index + 1, 'repeat': repeat,
                **result}

    def set_combo(self, button: int, usages: list[int], modifiers: int = 0):
        """Bind up to three keyboard HID usages plus Ctrl/Shift/Alt/GUI modifiers."""
        bounded(button, 1, 16, 'button matrix slot')
        bounded(modifiers, 0, 15, 'left modifier mask')
        if len(usages) > 3 or not (usages or modifiers):
            raise ValueError('use 1..3 keyboard usages and/or modifiers')
        for usage in usages:
            bounded(usage, 1, 255, 'keyboard usage')
        mods = [1 << bit for bit in range(4) if modifiers & (1 << bit)]
        records = ([bytes((0x80, mod, 0)) for mod in mods] +
                   [bytes((0x81, key, 0)) for key in usages] +
                   [bytes((0x40, mod, 0)) for mod in mods] +
                   [bytes((0x41, key, 0)) for key in reversed(usages)])
        if 2 + 3 * len(records) > 0x20:
            raise ValueError('combination exceeds its 32-byte slot; use a macro for more keys')
        self.write_eeprom(0x100 + (button - 1) * 0x20,
                          checked(bytes((len(records),)) + b''.join(records)))
        self.set_button(button, 5)

    def get_combo(self, button: int) -> dict:
        assignment = self.get_button(button)
        if assignment['code'] != 5:
            raise ProtocolError(f'Button slot {button} is not assigned to a combination')
        events = decode_combo(self.read_eeprom(0x100 + (button - 1) * 0x20, 0x20))
        return {'button': button, 'events': events}

    def get_profile(self) -> int:
        reply = self.command(0x0E)
        if reply[5] < 1:
            raise ProtocolError('Empty profile reply')
        return reply[6]

    def set_profile(self, index: int):
        self.command(0x10, data=bytes((bounded(index, 0, 255, 'profile index'),)))

    def profile(self, index: int | None = None) -> int | None:
        """Compatibility wrapper; prefer get_profile() and set_profile()."""
        if index is None:
            return self.get_profile()
        self.set_profile(index)
        return None

    def get_settings_raw(self) -> bytes:
        """Return the raw main settings EEPROM block (addresses 0x00-0xB4)."""
        return self.read_eeprom(0, 0xB5)

    def settings(self) -> dict:
        raw = self.get_settings_raw()
        def scalar(address):
            return check_record(raw[address:address + 2], f'0x{address:02x}')[0]
        count = scalar(2)
        if not 1 <= count <= 8:
            raise ProtocolError(f'Invalid stage count {count}')
        stages = []
        for i in range(count):
            x, y = decode_dpi(raw[0x0C + 4 * i:0x10 + 4 * i])
            color = check_record(raw[0x2C + 4 * i:0x30 + 4 * i], 'DPI color')
            stages.append({'stage': i + 1, 'x': x, 'y': y, 'rgb': list(color)})
        buttons = [decode_button(raw[0x60 + 4 * i:0x64 + 4 * i], i + 1)
                   for i in range(16)]
        return {'polling_hz': {v: k for k, v in RATES.items()}.get(scalar(0)),
                'stage_count': count, 'active_stage': scalar(4) + 1,
                'stages': stages, 'lod_mm': scalar(10),
                'dpi_effect': decode_dpi_effect(raw[0x4C:0x52]),
                'buttons': buttons, 'lighting': decode_lighting(raw[0xA0:0xA7]),
                'debounce_ms': scalar(0xA9), 'sleep_seconds': scalar(0xAD) * 10,
                'angle_snapping': bool(scalar(0xAF)),
                'ripple_control': bool(scalar(0xB1)),
                'light_off_moving': bool(scalar(0xB3))}

    def get_settings(self) -> dict:
        """Return all decoded records in the main settings block."""
        return self.settings()
