"""GPIO를 구동하지 않는 방향·한계 점멸·정지 연동 검사."""

import unittest
from unittest.mock import patch

from robot_vision.led import LEDState
from robot_vision.motor import Tracker
from robot_vision.tracking_controller import TrackingController


class FakeServo:
    def __init__(self):
        self.moves = []

    def move(self, angle):
        self.moves.append(angle)


class LEDTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.led = LEDState(clock=lambda: self.now)

    def values(self):
        state = self.led.snapshot()
        return state["right"], state["left"]

    def test_move_and_stop_outputs(self):
        self.assertEqual(self.values(), (False, False))
        self.led.set_state("right")
        self.assertEqual(self.values(), (True, False))
        self.led.set_state("left")
        self.assertEqual(self.values(), (False, True))
        self.led.set_state("stopped")
        self.assertEqual(self.values(), (False, False))

    def test_motion_without_new_commands_expires(self):
        self.led.set_state("right")
        self.now += 0.3
        self.led.set_state("right")
        self.now += 0.3
        self.assertEqual(self.values(), (True, False))
        self.now += 0.3
        self.assertEqual(self.values(), (False, False))

    def test_right_limit_keeps_left_on_and_blinks_each_second(self):
        self.led.set_state("right_limit")
        self.assertEqual(self.values(), (True, True))
        self.now += 1.1
        self.assertEqual(self.values(), (False, True))
        self.now += 1.0
        self.assertEqual(self.values(), (True, True))

    def test_left_limit_repeated_updates_do_not_restart_blink(self):
        self.led.set_state("left_limit")
        for _ in range(6):
            self.now += 0.2
            self.led.set_state("left_limit")
        self.assertEqual(self.values(), (True, False))
        self.now += 1.0
        self.assertEqual(self.values(), (True, True))

    def test_stop_cancels_limit_blink(self):
        self.led.set_state("left_limit")
        self.led.set_state("stopped")
        self.now += 5
        self.assertEqual(self.values(), (False, False))

    def test_reversed_servo_reports_camera_direction(self):
        servo = FakeServo()
        tracker = Tracker(servo, reverse=True, indicators=self.led)
        frame = dict(
            frame_id=1,
            timestamp=self.now,
            frame_width=640,
            frame_height=480,
            bbox=[450, 200, 550, 280],
        )
        with patch("robot_vision.motor.time.monotonic", side_effect=lambda: self.now):
            tracker.update(frame)
            self.now += 0.1
            frame.update(frame_id=2, timestamp=self.now)
            tracker.update(frame)
        self.assertLess(tracker.angle, 0)
        self.assertEqual(self.values(), (True, False))
        tracker.angle = -90
        tracker.result("angle_limit")
        self.assertEqual(self.led.snapshot()["state"], "right_limit")

    def test_stop_command_and_vision_error_turn_both_off(self):
        controller = TrackingController(FakeServo(), indicators=self.led)
        controller.apply_command(dict(action="look_at", color=None, object="person"))
        controller.tracker.angle = -90
        controller.tracker.result("angle_limit")
        self.assertEqual(self.led.snapshot()["state"], "left_limit")
        controller.apply_command(dict(action="stop", color=None, object=None))
        self.assertEqual(self.values(), (False, False))
        self.led.set_state("right")
        controller.hold("vision_error")
        self.assertEqual(self.values(), (False, False))

    def test_centered_at_limit_keeps_limit_pattern(self):
        tracker = Tracker(FakeServo(), indicators=self.led)
        tracker.angle = 90
        tracker.result("centered")
        self.assertEqual(self.led.snapshot()["state"], "right_limit")
        tracker.result("target_lost")
        self.assertEqual(self.values(), (False, False))


if __name__ == "__main__":
    unittest.main()
