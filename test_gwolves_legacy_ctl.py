"""Offline checks using recovered encodings and the actual captured battery reply."""
import unittest
from unittest.mock import patch

import gwolves_legacy_ctl as m


class ProtocolTests(unittest.TestCase):
    def test_battery_request_matches_disassembly(self):
        self.assertEqual(m.packet(4).hex(' '),
                         '08 04 00 00 00 00 00 00 00 00 00 00 00 00 00 00 49')

    def test_dpi_boundaries_and_separate_axes(self):
        for value in (50, 400, 800, 1600, 10000, 10100, 19000):
            record = m.encode_dpi(value)
            self.assertEqual(m.decode_dpi(record), (value, value))
        self.assertEqual(m.encode_dpi(1600), bytes.fromhex('1f 1f 00 17'))
        self.assertEqual(m.encode_dpi(19000), bytes.fromhex('bd bd 22 b9'))
        self.assertEqual(m.encode_dpi(800, 19000), bytes.fromhex('0f bd 20 69'))
        for value in (0, 49, 51, 10050, 19001, 20000):
            with self.assertRaises(ValueError):
                m.encode_dpi(value)

    def test_lod_packet_and_both_checksums(self):
        with patch.object(m.os, 'open', side_effect=AssertionError('hardware opened')):
            with m.Mouse(dry_run=True) as mouse:
                mouse.set_lod(2)
                self.assertEqual(mouse.planned_packets,
                                 ['08 07 00 00 0a 02 02 53 00 00 00 00 00 00 00 00 e5'])
                before = list(mouse.planned_packets)
                with self.assertRaises(ValueError):
                    mouse.set_lod(3)
                self.assertEqual(mouse.planned_packets, before)

    def test_stage_batch_preserves_four_byte_records(self):
        with m.Mouse(dry_run=True) as mouse:
            mouse.set_stages([400, 800, 1600], active=2)
            packets = [bytes.fromhex(p) for p in mouse.planned_packets]
        self.assertEqual([(int.from_bytes(p[3:5], 'big'), p[5]) for p in packets],
                         [(0x0C, 8), (0x14, 4), (2, 2), (4, 2)])
        self.assertEqual(packets[-1][6:8], bytes.fromhex('01 54'))
        for p in packets:
            self.assertEqual(sum(p) & 255, 0x55)

    def test_macro_layout_and_checksum_excludes_name(self):
        events = [{'type': 'key', 'code': 4, 'down': True, 'delay_ms': 50},
                  {'type': 'key', 'code': 4, 'down': False, 'delay_ms': 0}]
        record = m.encode_macro('A', events)
        self.assertEqual(record[:3], bytes.fromhex('02 41 00'))
        self.assertEqual(record[31:], bytes.fromhex('02 81 04 00 00 32 41 04 00 00 00 57 00 00 00'))
        self.assertEqual(sum(record[31:]) & 255, 0x55)
        self.assertEqual(m.encode_macro('B', events)[31:], record[31:])

    def test_packet_size_limits(self):
        for data in (bytes(11), bytes(17)):
            with self.assertRaises(ValueError):
                m.packet(7, data=data)
        with self.assertRaises(ValueError):
            m.packet(8, address=0x10000, read_length=1)

    def test_combo_cannot_overwrite_adjacent_slot(self):
        with m.Mouse(dry_run=True) as mouse:
            with self.assertRaisesRegex(ValueError, '32-byte'):
                mouse.set_combo(1, [4, 5, 6], modifiers=15)
            self.assertEqual(mouse.planned_packets, [])

    def test_decoded_button_combo_and_macro_values(self):
        self.assertEqual(
            m.decode_button(m.checked((0x0102).to_bytes(3, 'little')), 6),
            {'slot': 6, 'code': 0x0102, 'code_hex': '0x000102',
             'action': 'dpi-cycle'})

        combo = m.checked(bytes.fromhex(
            '04 80 01 00 81 06 00 40 01 00 41 06 00'))
        self.assertEqual(m.decode_combo(combo), [
            {'type': 'modifier', 'code': 1, 'down': True},
            {'type': 'key', 'code': 6, 'down': True},
            {'type': 'modifier', 'code': 1, 'down': False},
            {'type': 'key', 'code': 6, 'down': False},
        ])

        events = [{'type': 'key', 'code': 4, 'down': True, 'delay_ms': 50},
                  {'type': 'key', 'code': 4, 'down': False, 'delay_ms': 0}]
        decoded = m.decode_macro(m.encode_macro('A', events))
        self.assertEqual(decoded, {'name': 'A', 'events': events})

    def test_decoded_effect_values_match_setter_encodings(self):
        with m.Mouse(dry_run=True) as mouse:
            mouse.set_dpi_effect(2, brightness=5, speed=3)
            dpi_packet = bytes.fromhex(mouse.planned_packets[-1])
            mouse.set_lighting('breathing', (10, 20, 30), brightness=4, speed=6)
            lighting_packet = bytes.fromhex(mouse.planned_packets[-1])
            mouse.set_lighting('streaming', (1, 2, 3), brightness=9, speed=8)
            streaming_packet = bytes.fromhex(mouse.planned_packets[-1])

        self.assertEqual(m.decode_dpi_effect(dpi_packet[6:12]), {
            'mode': 2, 'brightness': 5, 'speed': 3,
            'brightness_raw': 128, 'speed_raw': 3})
        self.assertEqual(m.decode_lighting(lighting_packet[6:13]), {
            'mode': 'breathing', 'mode_raw': 1, 'rgb': [10, 20, 30],
            'brightness': 4, 'speed': 6,
            'brightness_raw': 100, 'speed_raw': 150})
        self.assertEqual(m.decode_lighting(streaming_packet[6:13]), {
            'mode': 'streaming', 'mode_raw': 0, 'rgb': [1, 2, 3],
            'brightness': 9, 'speed': 8,
            'brightness_raw': 232, 'speed_raw': 3})

    def emulate_battery(self, incoming):
        mouse = object.__new__(m.Mouse)
        mouse.fd = 999
        mouse.path = 'captured-hardware-response'
        mouse.timeout = 0.01
        mouse.verbose = False
        mouse.dry_run = False
        with patch.object(m.os, 'read', side_effect=[BlockingIOError(), *incoming]), \
             patch.object(m.fcntl, 'ioctl') as ioctl, \
             patch.object(m.select, 'select', return_value=([999], [], [])):
            result = mouse.battery()
            ioctl.assert_called_once_with(999, 0xC0114806, m.packet(4))
            return result

    def test_actual_battery_reply_and_unrelated_input_filtering(self):
        reply = bytes.fromhex('09 04 00 00 00 02 5a 00 00 00 00 00 00 00 00 00 ec')
        result = self.emulate_battery([bytes.fromhex('01 00 00'), reply])
        self.assertEqual(result['battery_percent'], 90)
        self.assertFalse(result['charging'])

    def test_connection_status_uses_receivers_wireless_state(self):
        mouse = object.__new__(m.Mouse)
        disconnected = m.checked(bytes.fromhex('09 03 00 00 00 01 00') + bytes(9))
        connected = m.checked(bytes.fromhex('09 03 00 00 00 01 01') + bytes(9))
        offline_status = m.checked(bytes.fromhex('09 03 01 00 00 00') + bytes(10))
        for reply, expected in ((disconnected, False), (offline_status, False),
                                (connected, True)):
            with patch.object(mouse, 'command', return_value=reply) as command:
                self.assertIs(mouse.is_connected(), expected)
                command.assert_called_once_with(0x03, allow_nonzero_status=True)
        invalid = m.checked(bytes.fromhex('09 03 00 00 00 01 02') + bytes(9))
        with patch.object(mouse, 'command', return_value=invalid):
            with self.assertRaisesRegex(m.ProtocolError, 'connection state'):
                mouse.is_connected()

    def test_timeout_notes_wireless_mouse_may_be_asleep(self):
        mouse = object.__new__(m.Mouse)
        mouse.fd = 999
        mouse.timeout = 0.01
        mouse.verbose = False
        mouse.dry_run = False
        with patch.object(m.os, 'read', side_effect=BlockingIOError()), \
             patch.object(m.fcntl, 'ioctl'), \
             patch.object(m.select, 'select', return_value=([], [], [])):
            with self.assertRaisesRegex(
                    TimeoutError, 'wireless the mouse may be asleep'):
                mouse.command(4)

    def test_corrupt_reply_is_not_reported_as_battery(self):
        reply = bytes.fromhex('09 04 00 00 00 02 5b 00 00 00 00 00 00 00 00 00 ec')
        with self.assertRaisesRegex(m.ProtocolError, 'checksum'):
            self.emulate_battery([reply])

    def test_invalid_percentage_is_not_reported_as_battery(self):
        reply = m.checked(bytes.fromhex('09 04 00 00 00 02 ff 00 00 00 00 00 00 00 00 00'))
        with self.assertRaisesRegex(m.ProtocolError, 'percentage'):
            self.emulate_battery([reply])


if __name__ == '__main__':
    unittest.main()
