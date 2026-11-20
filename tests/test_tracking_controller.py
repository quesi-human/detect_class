"""모터·마이크를 구동하지 않는 추적 연결부 검사."""

import math
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from robot_vision.microphone import send_tracking_command
from robot_vision.tracking_controller import ControlHandler, TargetSelector, TrackingController


def command(label="person", color=None):
    return {"action": "look_at", "object": label, "color": color}


def detection(box, label="person", confidence=0.9):
    return {"label": label, "confidence": confidence, "bbox": box}


class FakeServo:
    def __init__(self):
        self.moves = []

    def move(self, angle):
        self.moves.append(angle)


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.clock = patch(
            "robot_vision.tracking_controller.time.monotonic", side_effect=lambda: self.now
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.servo = FakeServo()
        self.controller = TrackingController(self.servo)

    def frame(self, frame_id=1, box=None):
        if box is None:
            box = [450, 200, 550, 280]
        return {
            "status": "ok",
            "stale": False,
            "frame_id": frame_id,
            "timestamp": self.now,
            "frame_width": 640,
            "frame_height": 480,
            "detections": [detection(box)],
        }

    def initialize(self):
        self.controller.apply_command(command())
        self.controller.process_snapshot(self.frame())

    def test_idle_does_not_move(self):
        self.controller.process_snapshot(self.frame())
        self.assertEqual(self.servo.moves, [])

    def test_new_frames_move_and_duplicates_do_not(self):
        self.initialize()
        self.assertEqual(self.servo.moves, [0])
        self.now += 0.2
        frame = self.frame(2)
        self.controller.process_snapshot(frame)
        self.assertGreater(self.servo.moves[-1], 0)
        count = len(self.servo.moves)
        self.now += 0.1
        self.controller.process_snapshot(frame)
        self.assertEqual(len(self.servo.moves), count)

    def test_stop_blocks_later_frames_without_returning_to_center(self):
        self.initialize()
        self.now += 0.2
        self.controller.process_snapshot(self.frame(2))
        moves = list(self.servo.moves)
        self.controller.apply_command({"action": "stop", "color": None, "object": None})
        self.now += 0.2
        self.controller.process_snapshot(self.frame(3))
        self.assertEqual(self.servo.moves, moves)
        self.assertEqual(self.controller.snapshot()["state"], "stopped")

    def test_stale_or_bad_frames_do_not_move(self):
        self.controller.apply_command(command())
        frame = self.frame()
        frame["timestamp"] -= 1
        self.controller.process_snapshot(frame)
        self.assertEqual(self.servo.moves, [])
        self.assertEqual(self.controller.snapshot()["state"], "stale")
        frame = self.frame()
        frame["detections"][0]["bbox"][0] = math.nan
        with self.assertRaises(ValueError):
            self.controller.process_snapshot(frame)
        self.assertEqual(self.servo.moves, [])

    def test_unsupported_request_keeps_previous_goal(self):
        self.controller.apply_command(command())
        for bad in [
            command("block"),
            command("cup", "yellow"),
            {"action": "unknown", "color": None, "object": None},
        ]:
            with self.assertRaises(ValueError):
                self.controller.apply_command(bad)
            self.assertEqual(self.controller.target, command())

    def test_missing_target_and_network_errors_hold_angle(self):
        self.initialize()
        moves = list(self.servo.moves)
        self.now += 0.2
        frame = self.frame(2)
        frame["detections"] = [detection([200, 100, 300, 200], "cup")]
        self.controller.process_snapshot(frame)
        self.assertEqual(self.controller.snapshot()["state"], "waiting_for_target")
        self.assertEqual(self.servo.moves, moves)
        self.controller.hold("vision_error", "timeout")
        self.assertEqual(self.servo.moves, moves)

    def test_missing_target_waits_without_moving_for_any_duration(self):
        self.initialize()
        moves = list(self.servo.moves)
        for i in range(100):
            self.now += 0.1
            frame = self.frame(i + 2)
            frame["detections"] = []
            self.controller.process_snapshot(frame)
        self.assertEqual(self.controller.state, "waiting_for_target")
        self.assertEqual(self.servo.moves, moves)
        self.now += 0.1
        self.controller.process_snapshot(self.frame(102))
        self.now += 0.1
        self.controller.process_snapshot(self.frame(103))
        self.assertEqual(self.controller.state, "tracking")
        self.assertGreater(len(self.servo.moves), len(moves))

    def test_stale_vision_never_runs_search(self):
        self.initialize()
        moves = list(self.servo.moves)
        self.now += 5
        frame = self.frame(2)
        frame["detections"] = []
        frame["timestamp"] -= 3
        self.controller.process_snapshot(frame)
        self.assertEqual(self.controller.state, "stale")
        self.assertEqual(self.servo.moves, moves)

    def test_server_restart_keeps_servo_angle(self):
        self.initialize()
        self.now += 0.2
        self.controller.process_snapshot(self.frame(100))
        angle = self.controller.tracker.angle
        self.now += 0.2
        self.controller.process_snapshot(self.frame(0))
        self.assertEqual(self.controller.tracker.angle, angle)
        self.assertEqual(self.controller.source_frame_id, 0)

    def test_reverse_changes_rotation_sign(self):
        self.controller = TrackingController(self.servo, reverse=True)
        self.initialize()
        self.now += 0.2
        self.controller.process_snapshot(self.frame(2))
        self.assertLess(self.servo.moves[-1], 0)

    def test_fast_updates_raise_response_without_exceeding_step_limit(self):
        self.controller.apply_command(command())
        self.controller.process_snapshot(self.frame(1, [600, 200, 640, 280]))
        for frame_id in range(2, 12):
            before = self.controller.tracker.angle
            self.now += 0.1
            self.controller.process_snapshot(self.frame(frame_id, [600, 200, 640, 280]))
            self.assertLessEqual(abs(self.controller.tracker.angle - before), 6.00001)
        self.assertGreater(self.controller.tracker.angle, 50)
        self.assertLessEqual(self.controller.tracker.angle, 60)

    def test_delayed_fresh_frame_cannot_cause_large_jump(self):
        self.controller.apply_command(command())
        self.controller.process_snapshot(self.frame(1, [600, 200, 640, 280]))
        self.now += 0.4
        self.controller.process_snapshot(self.frame(2, [600, 200, 640, 280]))
        self.assertAlmostEqual(self.controller.tracker.angle, 6)

    def test_near_center_slows_down(self):
        self.controller.apply_command(command())
        self.controller.process_snapshot(self.frame(1))
        self.now += 0.1
        self.controller.process_snapshot(self.frame(2, [342, 200, 362, 280]))
        self.assertGreater(self.controller.tracker.angle, 0)
        self.assertLess(self.controller.tracker.angle, 1)

    def test_selector_chooses_largest_box_each_frame(self):
        selector = TargetSelector()
        first = detection([260, 150, 360, 350], confidence=0.7)
        selected = selector.select([first], "person", 640, 480, self.now)
        nearby = detection([270, 150, 370, 350], confidence=0.6)
        other = detection([420, 100, 600, 400], confidence=0.99)
        selected = selector.select([other, nearby], "person", 640, 480, self.now + 0.2)
        self.assertEqual(selected["bbox"], other["bbox"])


class DeliveryTests(unittest.TestCase):
    def test_microphone_delivery_and_rejection(self):
        servo = FakeServo()
        server = ThreadingHTTPServer(("127.0.0.1", 0), ControlHandler)
        server.daemon_threads = True
        server.controller = TrackingController(servo)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/command"
            result = send_tracking_command(command(), url)
            self.assertEqual(result["mode"], "dry_run")
            self.assertEqual(server.controller.target, command())
            with self.assertRaises(RuntimeError):
                send_tracking_command(command("block"), url)
            self.assertEqual(server.controller.target, command())
            self.assertEqual(servo.moves, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
