import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from fleet_export import camera_offsets, playback_poses


def frame(stamp, x, yaw=0., hp=100):
    return stamp, [(1, x, 0., 0., yaw, 0., 0., hp)]


class PlaybackPoseTest(unittest.TestCase):
    def test_jitter_and_angle_wrap(self):
        frames = [frame(0., 0., math.pi-.03), frame(.1, 1.1, -math.pi),
                  frame(.2, 2., -math.pi+.01)]
        result = list(playback_poses(frames))
        self.assertEqual((result[0], result[-1]), (frames[0], frames[-1]))
        self.assertEqual(result[1][0], .1)
        pose, = result[1][1]
        self.assertAlmostEqual(pose[1], 1.05)
        self.assertAlmostEqual(math.remainder(pose[4]-math.pi, math.tau), -.005)

    def test_constant_speed_on_irregular_clock(self):
        frames = [frame(0., 0., 0.), frame(.08, .8, .08), frame(.2, 2., .2)]
        pose, = list(playback_poses(frames))[1][1]
        self.assertAlmostEqual(pose[1], .8)
        self.assertAlmostEqual(pose[4], .08)

    def test_discontinuities(self):
        for frames in [
            [frame(0., 0.), frame(.1, .9), frame(.2, .9)],
            [frame(0., 0.), frame(.1, .9), frame(.2, .1)],
            [frame(0., 0.), frame(.1, .9), frame(.2, 91., .1)],
            [frame(0., 0.), frame(.1, .9), frame(.5, 2.)],
            [frame(0., 0.), frame(.1, .9), (.2, [])],
            [frame(0., 0.), frame(.1, .9), frame(.2, 2., hp=0)],
        ]:
            with self.subTest(frames=frames):
                self.assertEqual(list(playback_poses(frames)), frames)

    def test_camera_filters_fixed_view_and_preserves_camera_cuts(self):
        axes = ((1,0,0), (0,1,0), (0,0,1))
        own = [(0., (0,0,0)), (.1, (1,0,0)), (.2, (2,0,0))]
        cameras = [(t, b'C', ['arcade', (x,1,-4), axes, 1., .1])
                   for (t, _), x in zip(own, (0, 1.2, 2))]
        self.assertAlmostEqual(list(camera_offsets(cameras, own))[1][0], .1)
        cameras[-1][2][0] = 'sniper'
        self.assertAlmostEqual(list(camera_offsets(cameras, own))[1][0], .2)
        cameras[-1][2][0] = 'arcade'
        cameras[-1][2][1] = (2,1,-12)
        self.assertAlmostEqual(list(camera_offsets(cameras, own))[1][0], .2)
