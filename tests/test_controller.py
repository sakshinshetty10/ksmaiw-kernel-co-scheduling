import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ksmaiw_core import HysteresisConfig, HysteresisController


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.c = HysteresisController(HysteresisConfig(high_ms=8, low_ms=5,
            enter_windows=2, exit_windows=3, min_samples=20, max_measurement_age_ms=750))

    def test_requires_two_high_windows(self):
        self.assertEqual(self.c.update(9, 40, 10)['new_state'], 'NORMAL')
        self.assertEqual(self.c.update(9, 40, 10)['new_state'], 'PROTECT')

    def test_requires_three_low_windows_to_exit(self):
        self.c.update(9, 40, 10)
        self.c.update(9, 40, 10)
        self.assertEqual(self.c.update(4, 40, 10)['new_state'], 'PROTECT')
        self.assertEqual(self.c.update(4, 40, 10)['new_state'], 'PROTECT')
        self.assertEqual(self.c.update(4, 40, 10)['new_state'], 'NORMAL')

    def test_stale_or_small_windows_do_not_switch(self):
        self.assertEqual(self.c.update(100, 19, 5)['new_state'], 'NORMAL')
        self.assertEqual(self.c.update(100, 100, 751)['new_state'], 'NORMAL')

    def test_transition_counter(self):
        self.c.update(9, 40, 10)
        row = self.c.update(9, 40, 10)
        self.assertTrue(row['changed'])
        self.assertEqual(row['transition_count'], 1)


if __name__ == '__main__':
    unittest.main()
