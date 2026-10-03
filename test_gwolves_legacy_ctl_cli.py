"""Hardware-free checks for the command-line layer."""
import contextlib
import io
import json
import unittest

import gwolves_legacy_ctl_cli as cli


class CliTests(unittest.TestCase):
    def run_cli(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = cli.main(list(argv))
        self.assertEqual(status, 0)
        return json.loads(output.getvalue())

    def test_dry_run_set_uses_library_without_opening_hardware(self):
        result = self.run_cli('--dry-run', 'set', 'dpi', '1600', '--stage', '1')
        self.assertTrue(result['dry_run'])
        self.assertEqual(result['packets'], [
            '08 07 00 00 0c 04 1f 1f 00 17 00 00 00 00 00 00 e1'])

    def test_get_parser_covers_decoded_fields(self):
        parser = cli.build_parser()
        for argv, field in ((['get', 'info'], 'info'),
                            (['get', 'battery'], 'battery'),
                            (['get', 'all'], 'all'),
                            (['get', 'dpi', '3'], 'dpi'),
                            (['get', 'lighting'], 'lighting'),
                            (['get', 'button', '5'], 'button'),
                            (['get', 'combo', '5'], 'combo'),
                            (['get', 'macro', '5'], 'macro')):
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertEqual(args.command, 'get')
                self.assertEqual(args.field, field)

    def test_get_info_returns_library_identity(self):
        class Device:
            def get_info(self):
                return {'connected': True, 'cid': 3, 'mid': 2, 'model': 'HSK Plus'}

        args = cli.build_parser().parse_args(['get', 'info'])
        self.assertEqual(cli.get_value(Device(), args),
                         {'connected': True, 'cid': 3, 'mid': 2, 'model': 'HSK Plus'})

    def test_get_all_reads_all_decoded_settings(self):
        class Device:
            def get_settings(self):
                return {'rate': 1000, 'sleep': 60}

        args = cli.build_parser().parse_args(['get', 'all'])
        self.assertEqual(cli.get_value(Device(), args),
                         {'rate': 1000, 'sleep': 60})


if __name__ == '__main__':
    unittest.main()
