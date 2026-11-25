import json
from pathlib import Path
import pickle
import struct
import sys
import unittest
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fleet_export import append_extra, append_periods


class ReplayFeedbackTest(unittest.TestCase):
    def test_public_kill_and_base_events_reach_the_native_arena(self):
        slots = {100: 7, 200: 9}
        extras = [(4., b'B', [8, 2, 1, 27, 73, 1, False]),
                  (6., b'B', [8, 2, 1, 0, 0, 0, False]),
                  (7., b'B', [9, 1, 2]), (8., b'J', [200, 100, 0, 0, 0])]
        packets = []
        append_extra(packets, extras, slots, [(4., [])], 4., 100, 17)
        bases = [(t, b[12], pickle.loads(b[14:])) for k,t,b in packets
                 if k == 8 and b[12] in (8, 9)]
        self.assertEqual(bases, [(0., 8, (2, 1, 27, 73, 1, False)),
                                 (2., 8, (2, 1, 0, 0, 0, False)), (3., 9, (1, 2))])
        death, = [b for k,t,b in packets if k == 0x31 and t == 4.]
        self.assertEqual(death[4:18], b'oh2Fleet.death')
        self.assertEqual(pickle.loads(death[22:]), (9, 7, 0, 0, 0))

    def test_damage_stickers_keep_the_exact_hit_and_restore_on_reentry(self):
        mark = (0, 0xdeadbeef12345678, 384032)
        encoded = struct.pack('<IQI', *mark)
        extras = [(7., b'K', [100, [mark]]), (8., b'K', [100, []]),
                  (12., b'K', [100, [mark]]), (13., b'K', [999, [mark]])]
        packets = []
        append_extra(packets, extras, {100: 18}, [(4., [])], 4., 100, 7)
        states = [(t, struct.unpack_from('<III', b), b[12:]) for k, t, b in packets if k == 7]
        self.assertEqual(states, [(3., (18, 33, 17), b'\x01' + encoded),
                                  (4., (18, 33, 1), b'\x00'),
                                  (8., (18, 33, 17), b'\x01' + encoded)])

    def test_completed_battle_keeps_winner_and_reason(self):
        for team in (0, 1, 2):
            with self.subTest(team=team):
                packets = []
                phases = [(0., b'P', [3, 900., 900.]), (12., b'P', [4, 5., 5., [team, 1]])]
                append_periods(packets, phases, [(0., []), (17., [])], 0., 7)
                finished, payload = [b for k, t, b in packets if k == 8 and t == 12.]
                self.assertEqual(finished, struct.pack('<IIIBB', 7, 10, 2, team, 1))
                self.assertEqual(pickle.loads(zlib.decompress(payload[14:])), (4, 17., 5., (team, 1)))

    def test_native_driver_controls_and_grouped_shot_results(self):
        packets = []
        extras = [(4., b'U', ['driverState', 32, True]), (5., b'U', ['driverState', 128, 2]),
                  (6., b'I', ['shots', [[100, 64, 1], [999, 8, 0], [101, 2, 0]]])]
        append_extra(packets, extras, {100: 18, 101: 27}, [(4., [])], 4., 100, 7)
        calls = []
        for kind, stamp, data in packets:
            if kind == 0x31:
                size, = struct.unpack_from('<I', data)
                name = data[4:4+size].decode('ascii')
                args = pickle.loads(data[8+size:])
                calls.append((stamp, name, args))
        self.assertEqual(calls[:2], [(0., 'client_vehicle_state_32', (32, True, 0)),
                                    (1., 'client_vehicle_state_128', (128, 2, 0))])
        stamp, name, (row,) = calls[2]
        self.assertEqual((stamp, name), (2., 'oh2Fleet.incoming'))
        self.assertEqual(json.loads(row), ['shots', [[18, 64, 1], [27, 2, 0]]])
