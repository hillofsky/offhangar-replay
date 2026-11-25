import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fleet_export import U32, append_periods


def replay_clock(phases):
    packets = [(0x15, 0., struct.pack('<d', 100.)), (0x16, 0., U32.pack(1))]
    extras = [(t, b'P', args) for t, *args in phases]
    append_periods(packets, extras, [(0., []), (30., [])], 0., 123)
    periods = [(t, U32.unpack(b)[0]) for k, t, b in packets if k == 0x16]
    ticks = {round(t, 3): struct.unpack('<f', b)[0] for k, t, b in packets if k == 0x17}
    return periods, ticks


class CountdownTest(unittest.TestCase):
    def test_loading_guard_from_older_track(self):
        periods, ticks = replay_clock([(0., 2, 45., 39.539), (15.919, 3, 900., 899.94)])
        self.assertEqual([p for _, p in periods], [2, 3])
        self.assertAlmostEqual(periods[1][0], 15.859)
        self.assertAlmostEqual(ticks[0], 15.859, places=4)
        self.assertEqual([int(ticks[t]) for t in range(16)], list(range(15, -1, -1)))
        self.assertEqual(ticks[15.859], 900.)
        self.assertEqual(ticks[16.859], 899.)

    def test_recorded_countdown_reset_and_custom_duration(self):
        periods, ticks = replay_clock([(0., 1, 0., 0.), (1.1, 2, 20., 19.9),
                                      (6.1, 2, 10., 9.9), (16.1, 3, 900., 899.9)])
        self.assertEqual([(round(t), p) for t, p in periods], [(0, 1), (1, 2), (6, 2), (16, 3)])
        self.assertEqual([ticks[t] for t in (0, 1, 5, 6, 15, 16)], [0, 20, 16, 10, 1, 900])

    def test_disabled_and_incomplete_countdown(self):
        periods, ticks = replay_clock([(0., 3, 900., 899.)])
        self.assertEqual(periods, [(0., 3)])
        self.assertEqual(ticks[0], 899.)
        periods, ticks = replay_clock([(0., 1, 0., 0.), (2., 2, 45., 45.)])
        self.assertEqual(periods, [(0., 1), (2., 2)])
        self.assertEqual(ticks[2], 45.)
        self.assertEqual(ticks[29], 18.)
