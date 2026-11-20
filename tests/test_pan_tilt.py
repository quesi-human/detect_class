"""두 축 독립 보정, 정지, 영상 유효성, 이동 한계 검사. 실제 GPIO 출력 없음."""

import unittest
from unittest.mock import patch

from robot_vision.motor import PanTiltTracker
from robot_vision.stack_service import commands
from robot_vision.tracking_controller import TrackingController


class Servo:
    def __init__(self):
        self.moves = []

    def move(self, angle):
        self.moves.append(angle)


class PanTiltTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.clock = patch("robot_vision.motor.time.monotonic", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.pan, self.tilt = Servo(), Servo()
        self.tracker = PanTiltTracker(self.pan, self.tilt)

    def update(self, box):
        self.now += 0.1
        return self.tracker.update(
            {
                "frame_id": round(self.now * 10),
                "timestamp": self.now,
                "frame_width": 640,
                "frame_height": 480,
                "bbox": box,
            }
        )

    def test_axes_use_different_coordinates(self):
        self.update([450, 350, 550, 450])
        self.update([450, 350, 550, 450])
        self.assertGreater(self.pan.moves[-1], 0)
        self.assertGreater(self.tilt.moves[-1], 0)
        old_pan = self.tracker.pan.angle
        self.update([300, 350, 340, 450])
        self.assertEqual(self.tracker.pan.angle, old_pan)
        self.assertGreater(self.tracker.tilt.angle, 0)

    def test_box_over_screen_center_still_moves_until_box_center_is_close(self):
        self.update([220, 150, 620, 450])
        self.update([220, 150, 620, 450])
        self.assertGreater(self.tracker.pan.angle, 0)
        self.assertGreater(self.tracker.tilt.angle, 0)
        before = self.tracker.pan.angle, self.tracker.tilt.angle
        result = self.update([310, 230, 330, 250])
        self.assertEqual(result["status"], "centered")
        self.assertEqual((self.tracker.pan.angle, self.tracker.tilt.angle), before)

    def test_independent_reverse_and_limits(self):
        self.tracker = PanTiltTracker(
            self.pan, self.tilt, reverse=True, tilt_reverse=False, pan_limit=4, tilt_limit=2
        )
        for _ in range(100):
            self.update([600, 440, 640, 480])
        self.assertEqual(self.tracker.pan.angle, -4)
        self.assertEqual(self.tracker.tilt.angle, 2)

    def test_lost_duplicate_stale_do_not_change_either_axis(self):
        self.update([450, 350, 550, 450])
        self.update([450, 350, 550, 450])
        counts = len(self.pan.moves), len(self.tilt.moves)
        self.update(None)
        data = {
            "frame_id": 99999,
            "timestamp": self.now - 2,
            "frame_width": 640,
            "frame_height": 480,
            "bbox": [450, 350, 550, 450],
        }
        self.assertEqual(self.tracker.update(data)["status"], "stale")
        self.assertEqual((len(self.pan.moves), len(self.tilt.moves)), counts)
        self.tracker.last_timestamp = None
        self.assertIsNone(self.tracker.tilt.last_timestamp)

    def test_controller_stop_holds_both_axes(self):
        c = TrackingController(self.pan, tilt_servo=self.tilt)
        c.apply_command({"action": "look_at", "color": None, "object": "person"})

        def frame(i):
            return {
                "status": "ok",
                "stale": False,
                "frame_id": i,
                "timestamp": self.now,
                "frame_width": 640,
                "frame_height": 480,
                "detections": [
                    {"label": "person", "confidence": 0.9, "bbox": [450, 350, 550, 450]}
                ],
            }

        c.process_snapshot(frame(1))
        self.now += 0.1
        c.process_snapshot(frame(2))
        before = list(self.pan.moves), list(self.tilt.moves)
        c.apply_command({"action": "stop", "color": None, "object": None})
        self.now += 0.1
        c.process_snapshot(frame(3))
        self.assertEqual((self.pan.moves, self.tilt.moves), before)
        self.assertEqual(set(c.snapshot()["axes"]), {"pan", "tilt"})

    def test_absent_target_holds_both_axes_and_new_object_resumes(self):
        c = TrackingController(self.pan, tilt_servo=self.tilt)
        c.apply_command({"action": "look_at", "color": None, "object": "person"})

        def frame(i, box=None):
            return {
                "status": "ok",
                "stale": False,
                "frame_id": i,
                "timestamp": self.now,
                "frame_width": 640,
                "frame_height": 480,
                "detections": []
                if box is None
                else [{"label": "person", "confidence": 0.9, "bbox": box, "track_id": str(i)}],
            }

        c.process_snapshot(frame(1, [450, 350, 550, 450]))
        self.now += 0.1
        c.process_snapshot(frame(2, [450, 350, 550, 450]))
        moves = list(self.pan.moves), list(self.tilt.moves)
        for i in range(3, 103):
            self.now += 0.1
            c.process_snapshot(frame(i))
        self.assertEqual(c.state, "waiting_for_target")
        self.assertEqual((self.pan.moves, self.tilt.moves), moves)
        for i in range(103, 105):
            self.now += 0.1
            c.process_snapshot(frame(i, [50, 50, 150, 150]))
        self.assertEqual(c.state, "tracking")
        self.assertNotEqual((self.pan.moves, self.tilt.moves), moves)

    def test_latest_command_tracks_actual_axis_updates(self):
        self.update([450, 350, 550, 450])
        result = self.update([450, 350, 550, 450])
        self.assertEqual(result["latest_command"]["motor"], "SG90")
        self.assertEqual(result["latest_command"]["direction"], "DOWN")
        result = self.update([450, 220, 550, 260])
        self.assertEqual(result["latest_command"]["motor"], "MG996R")
        self.assertEqual(result["latest_command"]["direction"], "RIGHT")
        result = self.update([300, 220, 340, 260])
        self.assertIsNone(result["latest_command"])

    def test_invalid_vertical_settings_rejected_before_service_start(self):
        for env in (
            {"TILT_REVERSE": "2"},
            {"TILT_MAX_SPEED": "nan"},
            {"TILT_LIMIT": "90"},
            {"PAN_LIMIT": "120"},
        ):
            with self.assertRaises(ValueError):
                commands(env)
        args = commands({"TILT_REVERSE": "1"})["tracking"]
        self.assertIn("--tilt-reverse", args)


if __name__ == "__main__":
    unittest.main()
