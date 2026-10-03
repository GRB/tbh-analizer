"""Ancestor walk used to tie the server to its launching terminal (no processes started)."""
import unittest

from tbh.lifetime import ancestors


class AncestorTests(unittest.TestCase):
    def test_stops_at_first_non_python(self):
        # explorer -> nexo -> shell -> venv launcher -> server
        table = {1: (0, 'explorer.exe'), 2: (1, 'nexo.exe'), 3: (2, 'pwsh.exe'),
                 4: (3, 'python.exe'), 5: (4, 'python.exe')}
        self.assertEqual(ancestors(table, 5), [(4, 'python.exe'), (3, 'pwsh.exe')])

    def test_orphan_has_no_owner(self):
        table = {4: (99, 'python.exe'), 5: (4, 'python.exe')}  # shell 99 already gone
        self.assertEqual(ancestors(table, 5), [(4, 'python.exe')])

    def test_parent_cycle_terminates(self):
        self.assertEqual(ancestors({5: (6, 'python.exe'), 6: (5, 'python.exe')}, 5), [(6, 'python.exe')])


if __name__ == '__main__':
    unittest.main()
