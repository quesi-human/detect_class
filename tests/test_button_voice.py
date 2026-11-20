"""버튼을 실제로 누르거나 녹음하지 않는 입력 게이트 검사."""

import argparse
import unittest

from robot_vision.button_voice import PressGate, microphone_command


class ButtonTests(unittest.TestCase):
    def test_starting_held_requires_release(self):
        gate = PressGate()
        self.assertFalse(gate.pressed())
        gate.released()
        self.assertTrue(gate.pressed())

    def test_repeated_press_during_processing_is_not_queued(self):
        gate = PressGate()
        gate.released()
        self.assertTrue(gate.pressed())
        gate.released()
        self.assertFalse(gate.pressed())
        gate.released()
        gate.finish(released=True)
        self.assertFalse(gate.event.is_set())
        self.assertTrue(gate.pressed())

    def test_holding_button_does_not_repeat(self):
        gate = PressGate()
        gate.released()
        gate.pressed()
        gate.finish(released=False)
        self.assertFalse(gate.pressed())
        gate.released()
        self.assertTrue(gate.pressed())

    def test_voice_child_does_not_take_button_pin(self):
        args = argparse.Namespace(
            device="hw:CARD=SPH0645,DEV=0",
            seconds=5,
            channel="left",
            gain=1,
            record_only=False,
            parse=False,
            send=True,
            output=None,
        )
        command = microphone_command(args)
        self.assertIn("--send", command)
        self.assertNotIn("--ptt", command)


if __name__ == "__main__":
    unittest.main()
