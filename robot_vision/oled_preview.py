"""기존 카메라 JPEG를 128×64 단색 OLED에 표시한다. 카메라는 따로 열지 않는다."""

import io
import json
import logging
import threading
import time
import urllib.request
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .object_catalog import SUPPORTED_OBJECTS, display_label


def motor_label(state):
    """추적 연결부의 모터 명령 상태를 OLED 문구로 바꾼다."""
    if state.get("mode") != "hardware":
        return "READY"
    if state.get("state") in {
        "stopped",
        "target_lost",
        "stale",
        "vision_error",
        "vision_unavailable",
        "invalid_detection",
        "control_error",
    }:
        return "STOP"
    if state.get("state") == "waiting_for_target":
        return "WAIT"
    if state.get("state") in {"idle", "initialized"}:
        return "READY"
    result = state.get("last_result") or {}
    if "latest_command" in result:
        command = result["latest_command"] or {}
        direction = command.get("direction")
        return direction if direction in {"LEFT", "RIGHT", "UP", "DOWN"} else "STOP"
    # 이전 형식의 상태 응답도 표시하되 MAX 문구는 사용하지 않는다.
    led_state = (state.get("led") or {}).get("state")
    return {"right": "RIGHT", "left": "LEFT"}.get(led_state, "STOP")


def motor_display(state):
    target = state.get("target")
    target_class = None
    if isinstance(target, dict) and target.get("action") == "look_at":
        value = target.get("object")
        if value in SUPPORTED_OBJECTS:
            target_class = value
    text = motor_label(state)
    label = "--" if target_class is None else display_label(target_class)
    return {"text": text, "target_class": target_class, "display_text": f"{text} | {label}"}


class MotorStatusReader:
    """별도 스레드에서 로컬 상태 API를 읽어 영상 표시를 막지 않는다."""

    def __init__(self, url="http://127.0.0.1:8082/status"):
        self.url = url
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.state = {
            "text": "READY",
            "display_text": "READY | --",
            "target_class": None,
            "online": False,
            "updated_at": None,
        }
        self.thread = threading.Thread(target=self.run, name="oled-motor-status", daemon=True)

    def start(self):
        self.thread.start()

    def snapshot(self):
        with self.lock:
            result = dict(self.state)
        updated = result.get("updated_at")
        if updated is not None and time.monotonic() - updated > 1:
            result.update(text="READY", display_text="READY | --", target_class=None, online=False)
        return result

    def run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                with urllib.request.urlopen(self.url, timeout=0.3) as response:
                    raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError("모터 상태 응답 크기 초과")
                state = json.loads(raw)
                if not isinstance(state, dict):
                    raise ValueError("모터 상태 응답 형식 오류")
                result = {
                    **motor_display(state),
                    "online": True,
                    "updated_at": time.monotonic(),
                    "mode": state.get("mode"),
                }
            except (OSError, ValueError, TypeError, AttributeError) as error:
                result = {
                    "text": "READY",
                    "display_text": "READY | --",
                    "target_class": None,
                    "online": False,
                    "updated_at": time.monotonic(),
                    "error": str(error),
                }
            with self.lock:
                self.state = result
            self.stop_event.wait(max(0, 0.2 - (time.monotonic() - started)))

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=1)


@lru_cache(maxsize=5)
def status_font(size=12):
    path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    return (
        ImageFont.truetype(str(path), size) if path.is_file() else ImageFont.load_default(size=size)
    )


def fit_status_font(draw, text, width):
    for size in range(12, 7, -1):
        font = status_font(size)
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= width - 4 and box[3] - box[1] <= 14:
            return font
    return font


def prepare_frame(jpeg, size=(128, 64), dither=True, status=None):
    with Image.open(io.BytesIO(jpeg)) as image:
        gray = image.convert("L")
    gray = ImageOps.autocontrast(gray, cutoff=1)
    video_size = (size[0], size[1] - 16) if status is not None else size
    scaled = ImageOps.contain(gray, video_size, method=Image.Resampling.LANCZOS)
    canvas = Image.new("L", size, 0)
    canvas.paste(scaled, ((size[0] - scaled.width) // 2, (video_size[1] - scaled.height) // 2))
    method = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    frame = canvas.convert("1", dither=method)
    if status is not None:
        draw = ImageDraw.Draw(frame)
        font = fit_status_font(draw, status, size[0])
        top = size[1] - 16
        draw.rectangle((0, top, size[0] - 1, size[1] - 1), fill=0)
        box = draw.textbbox((0, 0), status, font=font)
        x = (size[0] - (box[2] - box[0])) // 2 - box[0]
        y = top + (16 - (box[3] - box[1])) // 2 - box[1]
        draw.text((x, y), status, font=font, fill=255)
    return frame


class OLEDVideoWorker:
    def __init__(self, source, device, display_lock, fps=3, dither=True, motor_status=None):
        if not 0 < fps <= 10:
            raise ValueError("OLED FPS는 0 초과 10 이하여야 합니다.")
        self.source = source
        self.device = device
        self.display_lock = display_lock
        self.interval = 1 / fps
        self.dither = dither
        self.motor_status = motor_status
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.pause_until = 0
        self.state = {"status": "starting", "frames": 0, "frame_id": None}
        self.thread = threading.Thread(target=self.run, name="oled-video", daemon=True)

    def start(self):
        if self.motor_status is not None:
            self.motor_status.start()
        self.thread.start()

    def pause(self, seconds=3):
        with self.lock:
            self.pause_until = time.monotonic() + seconds

    def snapshot(self):
        with self.lock:
            state = dict(self.state)
            if self.pause_until > time.monotonic():
                state["status"] = "paused_for_text"
        displayed = state.get("displayed_at")
        state["display_age_ms"] = (
            None if displayed is None else round((time.monotonic() - displayed) * 1000, 1)
        )
        state["max_fps"] = round(1 / self.interval, 2)
        if self.motor_status is not None:
            state["motor_status"] = self.motor_status.snapshot()
        return state

    def run(self):
        last_frame_id = -1
        last_label = None
        while not self.stop_event.is_set():
            with self.lock:
                remaining = self.pause_until - time.monotonic()
            if remaining > 0:
                self.stop_event.wait(min(remaining, 0.1))
                continue
            with self.source.condition:
                self.source.condition.wait_for(
                    lambda: self.source.frame_id != last_frame_id or self.stop_event.is_set(),
                    timeout=0.2 if self.motor_status is not None else 1,
                )
                if self.stop_event.is_set():
                    break
                if self.source.frame is None:
                    continue
                frame_id = self.source.frame_id
                jpeg = self.source.frame
            started = time.monotonic()
            try:
                motor = None if self.motor_status is None else self.motor_status.snapshot()
                label = None if motor is None else motor.get("display_text", motor["text"])
                if frame_id == last_frame_id and label == last_label:
                    continue
                frame = prepare_frame(jpeg, self.device.size, self.dither, status=label)
                with self.display_lock:
                    # 준비 중 텍스트 요청이 들어왔으면 해당 텍스트를 덮어쓰지 않는다.
                    with self.lock:
                        paused = self.pause_until > time.monotonic()
                    if paused:
                        continue
                    self.device.display(frame)
                last_frame_id = frame_id
                last_label = label
                with self.lock:
                    self.state = {
                        "status": "running",
                        "frame_id": frame_id,
                        "frames": self.state["frames"] + 1,
                        "displayed_at": time.monotonic(),
                        "render_ms": round((time.monotonic() - started) * 1000, 2),
                        "status_text": label,
                    }
            except Exception as error:
                logging.exception("OLED 영상 표시 실패")
                with self.lock:
                    self.state.update(status="error", error=str(error))
                break
            self.stop_event.wait(max(0, self.interval - (time.monotonic() - started)))

    def close(self):
        self.stop_event.set()
        with self.source.condition:
            self.source.condition.notify_all()
        self.thread.join(timeout=3)
        if self.motor_status is not None:
            self.motor_status.close()
