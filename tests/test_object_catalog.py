"""실제 모델 클래스, 한국어 명령, 제어부, OLED의 허용 목록 일치 검사."""

import unittest
from pathlib import Path

import yaml
from PIL import Image, ImageDraw

from robot_vision.command_parser import validate
from robot_vision.object_catalog import SUPPORTED_OBJECTS, mentioned_objects
from robot_vision.oled_preview import fit_status_font, motor_display
from robot_vision.tracking_controller import SimulatedServo, TrackingController


class CatalogTests(unittest.TestCase):
    def test_supported_classes_match_export_metadata(self):
        metadata = Path(__file__).resolve().parent / "fixtures/yolo_metadata.yaml"
        names = yaml.safe_load(metadata.read_text())["names"]
        self.assertEqual(len(SUPPORTED_OBJECTS), 80)
        self.assertEqual(SUPPORTED_OBJECTS, set(names.values()))

    def test_every_canonical_english_name_is_recognized(self):
        for name in SUPPORTED_OBJECTS:
            with self.subTest(name=name):
                self.assertEqual(mentioned_objects("look at " + name), {name})

    def test_common_korean_commands_and_overlapping_names(self):
        for text, expected in [
            ("의자를 봐", "chair"),
            ("책을 따라가", "book"),
            ("휴대폰을 봐", "cell phone"),
            ("꽃병을 봐", "vase"),
            ("곰인형을 따라가", "teddy bear"),
            ("책가방을 봐", "backpack"),
            ("책상을 봐", "dining table"),
            ("사람을따라가", "person"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(mentioned_objects(text), {expected})
        self.assertEqual(mentioned_objects("컵과 병을 찾아"), {"cup", "bottle"})
        self.assertEqual(mentioned_objects("사랑할까?"), set())
        self.assertEqual(mentioned_objects("노란 블록을 봐"), set())
        self.assertEqual(mentioned_objects("병원을 봐"), set())

    def test_all_classes_accepted_by_controller_and_fit_oled(self):
        c = TrackingController(SimulatedServo())
        draw = ImageDraw.Draw(Image.new("1", (128, 64)))
        for name in SUPPORTED_OBJECTS:
            command = {"action": "look_at", "color": None, "object": name}
            self.assertEqual(validate(command), command)
            c.apply_command(command)
            state = {
                "mode": "hardware",
                "state": "tracking",
                "led": {"state": "right_limit"},
                "target": command,
            }
            result = motor_display(state)
            self.assertEqual(result["target_class"], name)
            font = fit_status_font(draw, result["display_text"], 128)
            bounds = draw.textbbox((0, 0), result["display_text"], font=font)
            self.assertLessEqual(bounds[2] - bounds[0], 124, name)
        with self.assertRaises(ValueError):
            c.apply_command({"action": "look_at", "color": None, "object": "block"})


if __name__ == "__main__":
    unittest.main()
