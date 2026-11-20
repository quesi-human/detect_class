"""서비스 실행 설정 검사. 프로세스나 하드웨어는 시작하지 않는다."""

import unittest

from robot_vision.stack_service import commands


class ServiceTests(unittest.TestCase):
    def test_default_pipeline_uses_package_modules(self):
        pipeline = commands({})
        self.assertEqual(set(pipeline), {"qwen", "camera", "tracking", "button"})
        self.assertIn("--hardware", pipeline["tracking"])
        self.assertNotIn("--reverse", pipeline["tracking"])
        self.assertIn("--send", pipeline["button"])
        self.assertNotIn("--ptt", pipeline["button"])
        self.assertIn("robot_vision.camera_server", pipeline["camera"])
        self.assertIn("--oled-video", pipeline["camera"])

    def test_direction_and_recording_config(self):
        pipeline = commands({"MOTOR_REVERSE": "1", "RECORD_SECONDS": "3"})
        self.assertIn("--reverse", pipeline["tracking"])
        self.assertEqual(pipeline["button"][-1], "3.0")

    def test_tracking_speed_and_camera_rates(self):
        pipeline = commands(
            {"TRACK_MAX_SPEED": "75", "TRACK_MAX_STEP": "5", "CAMERA_FPS": "20", "YOLO_FPS": "10"}
        )
        motor = pipeline["tracking"]
        self.assertEqual(motor[motor.index("--max-speed") + 1], "75.0")
        self.assertEqual(motor[motor.index("--max-step") + 1], "5.0")
        camera = pipeline["camera"]
        self.assertEqual(camera[camera.index("--fps") + 1], "20")
        self.assertEqual(camera[camera.index("--yolo-fps") + 1], "10.0")

    def test_invalid_config_is_rejected_before_start(self):
        for config in [
            {"RECORD_SECONDS": "99"},
            {"RECORD_SECONDS": "nan"},
            {"MOTOR_REVERSE": "--unexpected"},
            {"TRACK_MAX_SPEED": "nan"},
            {"TRACK_MAX_STEP": "16"},
            {"CAMERA_FPS": "5", "YOLO_FPS": "10"},
        ]:
            with self.assertRaises(ValueError):
                commands(config)


if __name__ == "__main__":
    unittest.main()
