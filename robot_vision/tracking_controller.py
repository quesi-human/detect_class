"""검증된 목표 명령 → 최신 YOLO 검출 → 하드웨어 PWM 추적 연결부.

기본은 모의 실행이다. --hardware를 지정한 경우에만 물리 33번(MG996R)과 32번(SG90)을 제어한다.
"""

import argparse
import fcntl
import json
import logging
import math
import signal
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .command_parser import validate
from .motor import HardwareServo, PanTiltTracker, Tracker
from .object_catalog import SUPPORTED_OBJECTS


class SimulatedServo:
    def move(self, angle):
        pass

    def release(self):
        pass


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("유한한 숫자가 필요합니다.")
    return float(value)


class TargetSelector:
    """매 프레임 지정 클래스에서 면적이 가장 큰 박스를 선택한다."""

    def select(self, detections, label, width, height, now):
        candidates = []
        for item in detections:
            if not isinstance(item, dict) or item.get("label") != label:
                continue
            confidence = finite_number(item.get("confidence"))
            if not 0 <= confidence <= 1:
                raise ValueError("검출 신뢰도 범위 오류")
            if confidence < 0.4:
                continue
            box = item.get("bbox")
            if not isinstance(box, list) or len(box) != 4:
                raise ValueError("검출 bbox 형식 오류")
            box = list(map(finite_number, box))
            if not (0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
                raise ValueError("검출 bbox 범위 오류")
            candidates.append({"label": label, "confidence": confidence, "bbox": box})
        if not candidates:
            return None

        def priority(item):
            x1, y1, x2, y2 = item["bbox"]
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            return (
                (x2 - x1) * (y2 - y1),
                item["confidence"],
                -math.hypot(cx - width / 2, cy - height / 2),
            )

        return max(candidates, key=priority)


class TrackingController:
    def __init__(
        self,
        servo,
        hardware=False,
        reverse=False,
        indicators=None,
        max_speed=60.0,
        max_step=6.0,
        tilt_servo=None,
        tilt_reverse=False,
        tilt_speed=10,
        tilt_step=1,
        pan_limit=60,
        tilt_limit=20,
    ):
        self.servo = servo
        self.indicators = indicators
        self.tracker = Tracker(
            servo, reverse=reverse, indicators=indicators, max_speed=max_speed, max_step=max_step
        )
        if tilt_servo is not None:
            self.tracker = PanTiltTracker(
                servo,
                tilt_servo,
                reverse,
                indicators,
                max_speed,
                max_step,
                tilt_reverse,
                tilt_speed,
                tilt_step,
                pan_limit,
                tilt_limit,
            )
        self.hardware = hardware
        self.lock = threading.RLock()
        self.target = None
        self.selector = TargetSelector()
        self.state = "idle"
        self.error = None
        self.last_source_timestamp = None
        self.source_frame_id = None
        self.sequence = 0
        self.last_result = None
        self.selected = None

    def apply_command(self, command):
        command = validate(command)
        if command["action"] == "unknown":
            raise ValueError("해석되지 않은 명령은 실행하지 않습니다.")
        if command["action"] == "look_at":
            if command["object"] not in SUPPORTED_OBJECTS:
                raise ValueError("현재 YOLO 모델이 지원하지 않는 클래스입니다.")
            if command["color"] is not None:
                raise ValueError("색상 판별은 아직 지원하지 않습니다.")
        with self.lock:
            self.target = dict(command) if command["action"] == "look_at" else None
            self.selector = TargetSelector()
            self.selected = None
            self.tracker.last_timestamp = None
            if self.indicators is not None:
                self.indicators.set_state("stopped")
            self.state = "waiting_for_target" if self.target else "stopped"
            self.error = None
            return {
                "ok": True,
                "status": "accepted",
                "mode": self.mode,
                "target": self.target,
                "state": self.state,
            }

    @property
    def mode(self):
        return "hardware" if self.hardware else "dry_run"

    def hold(self, state, error=None):
        with self.lock:
            self.tracker.last_timestamp = None
            if self.indicators is not None:
                self.indicators.set_state("stopped")
            self.selected = None
            self.error = error
            # 정지 요청이 네트워크 오류에 의해 가려지지 않게 유지한다.
            if self.target is not None:
                self.state = state

    def process_snapshot(self, data):
        with self.lock:
            if self.target is None:
                return
            if (
                not isinstance(data, dict)
                or data.get("status") != "ok"
                or data.get("stale") is True
            ):
                self.hold("vision_unavailable")
                return
            timestamp = finite_number(data.get("timestamp"))
            now = time.monotonic()
            age = now - timestamp
            if age < -0.1 or age > self.tracker.max_age:
                self.hold("stale")
                return
            frame_id = data.get("frame_id")
            if type(frame_id) is not int or frame_id < 0:
                raise ValueError("영상 frame_id 오류")
            if self.last_source_timestamp is not None and timestamp <= self.last_source_timestamp:
                return
            width = finite_number(data.get("frame_width"))
            height = finite_number(data.get("frame_height"))
            if width <= 0 or height <= 0:
                raise ValueError("영상 크기 오류")
            detections = data.get("detections")
            if not isinstance(detections, list):
                raise ValueError("검출 목록 형식 오류")

            if self.source_frame_id is not None and frame_id < self.source_frame_id:
                # 영상 서버 재시작: 각도는 유지하고 프레임/대상 연결 상태만 초기화.
                self.selector = TargetSelector()
                self.tracker.last_timestamp = None
            selected = self.selector.select(
                detections,
                self.target["object"],
                width,
                height,
                now,
            )
            self.sequence += 1
            if selected is None:
                self.hold("waiting_for_target")
                result = {
                    "ok": True,
                    "status": "waiting_for_target",
                    "target_angle": round(self.tracker.angle, 2),
                }
            else:
                result = self.tracker.update(
                    {
                        "frame_id": self.sequence,
                        "timestamp": timestamp,
                        "frame_width": width,
                        "frame_height": height,
                        "bbox": selected["bbox"],
                    }
                )
            self.last_source_timestamp = timestamp
            self.source_frame_id = frame_id
            self.selected = selected
            self.last_result = result
            self.error = None
            self.state = result["status"]

    def snapshot(self):
        with self.lock:
            age = (
                None
                if self.last_source_timestamp is None
                else time.monotonic() - self.last_source_timestamp
            )
            return {
                "mode": self.mode,
                "state": self.state,
                "target": self.target,
                "axes": self.tracker.axes() if isinstance(self.tracker, PanTiltTracker) else None,
                "selection_mode": "largest_bbox",
                "target_angle": round(self.tracker.angle, 2),
                "selected": self.selected,
                "source_frame_id": self.source_frame_id,
                "source_age_ms": None if age is None else round(max(0, age) * 1000, 1),
                "last_result": self.last_result,
                "error": self.error,
                "led": None if self.indicators is None else self.indicators.snapshot(),
                "max_speed": self.tracker.max_speed,
                "max_step": self.tracker.max_step,
            }


class VisionPoller(threading.Thread):
    def __init__(self, controller, url):
        super().__init__(name="vision-poll", daemon=True)
        self.controller = controller
        self.url = url
        self.stop_event = threading.Event()

    def run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            with self.controller.lock:
                active = self.controller.target is not None
            if active:
                try:
                    request = urllib.request.Request(
                        self.url, headers={"Cache-Control": "no-cache"}
                    )
                    with urllib.request.urlopen(request, timeout=1) as response:
                        raw = response.read(256_001)
                    if len(raw) > 256_000:
                        raise ValueError("검출 응답이 너무 큽니다.")
                    data = json.loads(raw)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    self.controller.hold("vision_error", str(error))
                else:
                    try:
                        self.controller.process_snapshot(data)
                    except (ValueError, KeyError, TypeError) as error:
                        self.controller.hold("invalid_detection", str(error))
                    except Exception:
                        # PWM 쓰기 등 제어 오류 후에는 새 보정을 중단한다.
                        logging.exception("추적 처리 실패")
                        self.controller.apply_command(
                            {"action": "stop", "color": None, "object": None}
                        )
                        with self.controller.lock:
                            self.controller.state = "control_error"
                            self.controller.error = "추적 처리 실패; 정지했습니다."
            self.stop_event.wait(max(0, 0.1 - (time.monotonic() - started)))


class ControlHandler(BaseHTTPRequestHandler):
    def reply(self, body, status=200):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/status":
            self.reply(self.server.controller.snapshot())
        else:
            self.reply({"error": "not_found"}, 404)

    def do_POST(self):
        if self.path != "/command":
            self.reply({"error": "not_found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("명령 크기는 1~4096바이트여야 합니다.")
            self.connection.settimeout(2)
            command = json.loads(self.rfile.read(length))
            self.reply(self.server.controller.apply_command(command))
        except (ValueError, TypeError, OSError) as error:
            self.reply({"ok": False, "error": str(error)}, 400)

    def log_message(self, format, *args):
        if self.command == "GET" and self.path == "/status":
            return
        logging.info(format, *args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardware", action="store_true", help="실제 서보 제어 활성화")
    parser.add_argument("--reverse", action="store_true", help="좌우 보정 방향 반전")
    parser.add_argument("--max-speed", type=float, default=30)
    parser.add_argument("--max-step", type=float, default=3)
    parser.add_argument("--tilt-reverse", action="store_true")
    parser.add_argument("--tilt-speed", type=float, default=10)
    parser.add_argument("--tilt-step", type=float, default=1)
    parser.add_argument("--pan-limit", type=float, default=60)
    parser.add_argument("--tilt-limit", type=float, default=20)
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--vision-url", default="http://127.0.0.1:8080/detections.json")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("포트 범위 오류")
    logging.basicConfig(level=logging.INFO)
    servo = tilt_servo = server = poller = lock_file = indicators = None
    try:
        # 포트 충돌을 먼저 확인하여 시작 실패 때 PWM에 접근하지 않는다.
        server = ThreadingHTTPServer(("127.0.0.1", args.port), ControlHandler)
        server.daemon_threads = True
        if args.hardware:
            lock_file = open("/run/lock/pjt-robot-vision-servo.lock", "w")
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            servo = HardwareServo(channel=1)
            tilt_servo = HardwareServo(channel=0)
            from .led import MotionLEDs

            indicators = MotionLEDs()
        else:
            servo = SimulatedServo()
            tilt_servo = SimulatedServo()
        controller = TrackingController(
            servo,
            args.hardware,
            args.reverse,
            indicators,
            args.max_speed,
            args.max_step,
            tilt_servo,
            args.tilt_reverse,
            args.tilt_speed,
            args.tilt_step,
            args.pan_limit,
            args.tilt_limit,
        )
        server.controller = controller
        poller = VisionPoller(controller, args.vision_url)
        poller.start()

        def interrupt(*_):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupt)
        logging.info(
            "연결부 시작: http://127.0.0.1:%d, 모드=%s, 목표 입력 대기", args.port, controller.mode
        )
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        if poller is not None:
            poller.stop_event.set()
            poller.join(timeout=2)
        if server is not None:
            server.server_close()
        try:
            try:
                if servo is not None:
                    servo.release()
            finally:
                if tilt_servo is not None:
                    tilt_servo.release()
        finally:
            if indicators is not None:
                indicators.close()
            if lock_file is not None:
                lock_file.close()


if __name__ == "__main__":
    main()
