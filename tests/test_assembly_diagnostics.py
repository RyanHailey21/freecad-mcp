"""Run using the existing FreeCAD MCP environment; no CAD execution required."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from freecad_mcp.assembly_diagnostics import diagnose


class Diagnostics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input.step'
        self.source.write_text('fixture')

    def test_timeout_report_is_durable_and_input_preserved(self):
        with patch('freecad_mcp.headless.run_headless', return_value={'success':False,'timed_out':True}):
            report = diagnose(str(self.source), str(self.root), command=['unused'])
        self.assertEqual(report['status'], 'timeout')
        self.assertEqual(json.loads(Path(report['report_path']).read_text())['status'], 'timeout')
        self.assertEqual(self.source.read_text(), 'fixture')

    def test_zero_exit_without_report_is_error(self):
        with patch('freecad_mcp.headless.run_headless', return_value={'success':True}):
            report = diagnose(str(self.source), str(self.root), command=['unused'])
        self.assertEqual(report['status'], 'error')

    def test_invalid_budget(self):
        for budget in (0, -1, 61, float('nan')):
            with self.assertRaises(ValueError):
                diagnose(str(self.source), str(self.root), timeout=budget)


if __name__ == '__main__':
    unittest.main()
