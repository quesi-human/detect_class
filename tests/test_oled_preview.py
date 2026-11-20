"""화면을 실제로 구동하지 않는 OLED 영상 변환·공유 프레임 검사."""

import io
import threading
import time
import unittest

from PIL import Image, ImageDraw

from robot_vision.oled_preview import (
    OLEDVideoWorker,
    fit_status_font,
    motor_display,
    motor_label,
    prepare_frame,
)


def jpeg(color="white"):
    buffer = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(buffer, format="JPEG")
    return buffer.getvalue()


class Source:
    def __init__(self):
        self.condition = threading.Condition()
        self.frame = None
        self.frame_id = -1

    def publish(self, frame_id, frame):
        with self.condition:
            self.frame_id, self.frame = frame_id, frame
            self.condition.notify_all()


class Screen:
    size = (128, 64)

    def __init__(self):
        self.ready = threading.Event()
        self.images = []

    def display(self, image):
        self.images.append(image)
        self.ready.set()


class OLEDTests(unittest.TestCase):
    def test_status_and_target_class_are_combined(self):
        state = {
            "mode": "hardware",
            "state": "tracking",
            "led": {"state": "right"},
            "target": {"action": "look_at", "color": None, "object": "person"},
        }
        self.assertEqual(motor_display(state)["display_text"], "RIGHT | PERSON")
        state["state"] = "target_lost"
        self.assertEqual(motor_display(state)["display_text"], "STOP | PERSON")
        state.update(state="stopped", target=None)
        self.assertEqual(motor_display(state)["display_text"], "STOP | --")
        state["state"] = "idle"
        self.assertEqual(motor_display(state)["display_text"], "READY | --")

    def test_longest_footer_fits_without_clipping(self):
        draw = ImageDraw.Draw(Image.new("1", (128, 64)))
        for text in ["RIGHT | PERSON", "UP | BOTTLE", "READY | PERSON"]:
            font = fit_status_font(draw, text, 128)
            box = draw.textbbox((0, 0), text, font=font)
            self.assertLessEqual(box[2] - box[0], 124)
            self.assertLessEqual(box[3] - box[1], 14)
            image = prepare_frame(jpeg(), status=text)
            self.assertIsNotNone(image.crop((0, 48, 128, 64)).getbbox())

    def test_footer_reserves_bottom_16_pixels(self):
        image = prepare_frame(jpeg(), dither=False, status="READY")
        self.assertEqual(image.size, (128, 64))
        self.assertEqual(image.crop((0, 0, 128, 48)).getbbox(), (32, 0, 96, 48))
        self.assertIsNotNone(image.crop((0, 48, 128, 64)).getbbox())
        self.assertEqual(image.getpixel((0, 63)), 0)

    def test_status_mapping_and_stop_priority(self):
        for state, label in [
            ("right", "RIGHT"),
            ("left", "LEFT"),
            ("right_limit", "STOP"),
            ("left_limit", "STOP"),
            ("stopped", "STOP"),
        ]:
            self.assertEqual(
                motor_label({"mode": "hardware", "state": "tracking", "led": {"state": state}}),
                label,
            )
        self.assertEqual(
            motor_label({"mode": "hardware", "state": "stopped", "led": {"state": "right_limit"}}),
            "STOP",
        )
        self.assertEqual(motor_label({"mode": "hardware", "state": "idle"}), "READY")
        self.assertEqual(
            motor_label({"mode": "dry_run", "state": "tracking", "led": {"state": "right"}}),
            "READY",
        )

    def test_latest_vertical_command_overrides_horizontal_led_state(self):
        state = {
            "mode": "hardware",
            "state": "tracking",
            "led": {"state": "right_limit"},
            "target": {"action": "look_at", "color": None, "object": "person"},
            "last_result": {"latest_command": {"motor": "SG90", "axis": "tilt", "direction": "UP"}},
        }
        self.assertEqual(motor_display(state)["display_text"], "UP | PERSON")
        state["last_result"]["latest_command"] = {
            "motor": "MG996R",
            "axis": "pan",
            "direction": "LEFT",
        }
        self.assertEqual(motor_display(state)["display_text"], "LEFT | PERSON")
        state["last_result"]["latest_command"] = None
        self.assertEqual(motor_display(state)["display_text"], "STOP | PERSON")
        state["state"] = "waiting_for_target"
        self.assertEqual(motor_display(state)["display_text"], "WAIT | PERSON")

    def test_resolution_and_aspect_ratio(self):
        image = prepare_frame(jpeg(), dither=False)
        self.assertEqual(image.mode, "1")
        self.assertEqual(image.size, (128, 64))
        self.assertEqual(image.getbbox(), (21, 0, 106, 64))

    def test_black_frame_remains_black(self):
        self.assertIsNone(prepare_frame(jpeg("black")).getbbox())

    def test_dither_creates_binary_halftone(self):
        image = prepare_frame(jpeg((128, 128, 128)))
        self.assertEqual(set(image.crop((22, 1, 105, 63)).getdata()), {0, 255})

    def test_worker_uses_latest_frame_and_stops(self):
        source, screen = Source(), Screen()
        worker = OLEDVideoWorker(source, screen, threading.Lock(), fps=10)
        source.publish(1, jpeg())
        worker.start()
        try:
            self.assertTrue(screen.ready.wait(2))
            # 검출·카메라 스레드의 중간 프레임을 큐로 쌓지 않는다.
            with source.condition:
                source.frame_id, source.frame = 3, jpeg("black")
                source.condition.notify_all()
            deadline = time.monotonic() + 2
            while worker.snapshot()["frame_id"] != 3 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(worker.snapshot()["frame_id"], 3)
            self.assertEqual(screen.images[-1].mode, "1")
        finally:
            worker.close()
        self.assertFalse(worker.thread.is_alive())

    def test_text_pause_delays_video(self):
        source, screen = Source(), Screen()
        worker = OLEDVideoWorker(source, screen, threading.Lock(), fps=10)
        worker.pause(0.3)
        source.publish(1, jpeg())
        worker.start()
        try:
            self.assertFalse(screen.ready.wait(0.1))
            self.assertTrue(screen.ready.wait(2))
        finally:
            worker.close()

    def test_status_updates_without_new_camera_frame(self):
        class Status:
            text = "READY"

            def start(self):
                pass

            def snapshot(self):
                return {"text": self.text}

            def close(self):
                pass

        status = Status()
        source, screen = Source(), Screen()
        worker = OLEDVideoWorker(source, screen, threading.Lock(), fps=10, motor_status=status)
        source.publish(1, jpeg())
        worker.start()
        try:
            self.assertTrue(screen.ready.wait(2))
            status.text = "RIGHT"
            deadline = time.monotonic() + 2
            while worker.snapshot().get("status_text") != "RIGHT" and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(worker.snapshot()["status_text"], "RIGHT")
            self.assertEqual(worker.snapshot()["frame_id"], 1)
        finally:
            worker.close()


if __name__ == "__main__":
    unittest.main()
