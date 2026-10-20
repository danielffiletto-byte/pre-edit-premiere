from fractions import Fraction
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'ferramentas'/'premiere'))
from exportar_premiere import verified_cadence, media_timing, timeline_grid, VideoTimingError


class CadenceTests(unittest.TestCase):
    def test_exact_thirty(self):
        rate, detail = verified_cadence([20*i for i in range(1040)], '1/600', '30/1', '30/1')
        self.assertEqual(rate, 30)
        self.assertEqual(detail['max_deviation_ms'], 0)

    def test_iphone_average_does_not_define_cadence(self):
        pts = [20*i + min(3, i//250) for i in range(1040)]
        rate, detail = verified_cadence(pts, '1/600', '30/1', '624000/20803')
        self.assertEqual(rate, 30)
        self.assertAlmostEqual(detail['max_deviation_ms'], 5)
        self.assertFalse(detail['video_reencoded'])

    def test_ntsc_is_not_rounded_to_thirty(self):
        rate, _ = verified_cadence(list(range(2000)), '1001/30000', '30000/1001', '30000/1001')
        self.assertEqual(rate, Fraction(30000, 1001))

    def test_frame_drop_is_rejected(self):
        pts = [20*i+(20 if i > 100 else 0) for i in range(300)]
        with self.assertRaisesRegex(ValueError, 'variável'):
            verified_cadence(pts, '1/600', '30', '30')

    def test_cumulative_drift_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'variável'):
            verified_cadence([201*i for i in range(300)], '1/6000', '30', '30')

    def test_duplicate_timestamp_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duplicados'):
            verified_cadence([0, 20, 20, 40], '1/600', '30', '30')

    def test_vfr_uses_elapsed_time_not_number_of_images(self):
        pts = [20*i for i in range(120) if i != 45]
        timing = media_timing(pts, '1/600', '30', '119/4', '4')
        self.assertEqual(timing['frames'], 120)
        self.assertEqual(timing['duration'], 4)
        self.assertEqual(timing['media_fps'], '119/4')
        self.assertEqual(timing['cadence']['mode'], 'vfr_original')
        self.assertFalse(timing['cadence']['video_reencoded'])

    def test_iphone_drift_preserves_actual_duration(self):
        timing = media_timing([201*i for i in range(300)], '1/6000', '30', '6000/201', '10.05')
        self.assertEqual(timing['duration'], 10.05)
        self.assertEqual(timing['frames'], 301)
        self.assertEqual(timing['cadence']['mode'], 'vfr_original')

    def test_source_duration_cannot_precede_last_timestamp(self):
        with self.assertRaisesRegex(ValueError, 'inconsistentes'):
            media_timing([0,20,40,60], '1/600', '30', '30', '0.1')

    def test_vfr_does_not_allow_duplicate_pts(self):
        with self.assertRaisesRegex(VideoTimingError, 'repetidos'):
            media_timing([0,20,20,40], '1/600', '30', '30', '1')

    def test_mixed_rates_share_grid_not_playback_speed(self):
        source = {'fps': '30000/1001', 'media_fps': '30000/1001', 'duration': 4.004,
                  'cadence': {'mode': 'regular'}, 'frames': 120}
        changed = timeline_grid(source, '30')
        self.assertEqual(changed['fps'], '30')
        self.assertEqual(changed['media_fps'], source['media_fps'])
        self.assertEqual(changed['duration'], source['duration'])
        self.assertEqual(source['fps'], '30000/1001')
        self.assertLess(changed['cadence']['tail_outside_grid_ms'], 1000/30)


if __name__ == '__main__':
    unittest.main()
