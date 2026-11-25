from pathlib import Path
import struct
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fleet_export import append_destructibles


class DestructiblesTest(unittest.TestCase):
    def test_chunk_position_timing_and_duplicate_module(self):
        chunk = (125 << 8) | 130
        packets = []
        rows = [(3., b'D', (chunk, 3, [0, 12, 146])),
                (4., b'D', (chunk, 3, [0, 12, 147])),
                (5., b'D', (chunk, 3, [0, 12, 149])),
                (2., b'D', (chunk + 1, 0, [1, 4, 128, 2, 70]))]
        append_destructibles(packets, rows, bytes(46), 2.)
        spawns = [data for kind, _, data in packets if kind == 5]
        self.assertEqual(len(spawns), 2)
        self.assertEqual([struct.unpack_from('<3f', s, 18) for s in spawns],
                         [(-150., 0., 450.), (-150., 0., 350.)])
        self.assertTrue(all(s[46:] == bytes.fromhex('040000010002000300') for s in spawns))
        updates = [(stamp, struct.unpack_from('<III', data), data[12:])
                   for kind, stamp, data in packets if kind == 7]
        area = 0x07000000 + chunk
        self.assertEqual(updates, [
            (0., (area + 1, 3, 6), bytes([1, 1, 4, 128, 2, 70])),
            (1., (area, 0, 4), bytes([1, 0, 12, 146])),
            (3., (area, 0, 7), bytes([2, 0, 12, 146, 0, 12, 149]))])
