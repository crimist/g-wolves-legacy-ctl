"""Regression checks for offline receiver replies in the battery logger."""

import csv
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from examples.battery_logger import log_battery


class FakeMouse:
    def __init__(self, states):
        self.states = iter(states)
        self.battery_reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def is_connected(self):
        return next(self.states)

    def get_battery(self):
        self.battery_reads += 1
        return {'battery_percent': 55, 'charging': False}


class LoggerTests(unittest.TestCase):
    def test_only_online_battery_samples_are_written(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'battery.csv'
            for states, reads, rows in (
                    ([False], 0, 0),
                    ([True, False], 1, 0),
                    ([True, True], 1, 1)):
                mouse = FakeMouse(states)
                output_text = StringIO()
                with patch.object(log_battery, 'Mouse', return_value=mouse), \
                     patch.object(log_battery.sys, 'argv',
                                  ['log_battery.py', '--once', '--output', str(output)]), \
                     redirect_stdout(output_text):
                    self.assertEqual(log_battery.main(), 0)
                self.assertEqual(mouse.battery_reads, reads)
                if rows == 0:
                    self.assertFalse(output.exists())
                    self.assertIn('mouse offline; no battery sample',
                                  output_text.getvalue())
                else:
                    with output.open(newline='') as file:
                        lines = list(csv.DictReader(file))
                    self.assertEqual(len(lines), rows)
                    self.assertEqual(lines[0]['battery_percent'], '55')
                    self.assertIn('55%', output_text.getvalue())


if __name__ == '__main__':
    unittest.main()
