"""카메라·Qwen·추적·버튼 프로세스의 시작 순서와 종료를 관리한다."""

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from .paths import PROJECT_ROOT as ROOT

PYTHON = str(ROOT / ".venv/bin/python")


def commands(environment=None):
    env = os.environ if environment is None else environment
    seconds = float(env.get("RECORD_SECONDS", "5"))
    if not 0.3 <= seconds <= 15:
        raise ValueError("RECORD_SECONDS는 0.3~15초 범위입니다.")
    reverse = env.get("MOTOR_REVERSE", "0").lower()
    if reverse not in {"0", "1", "false", "true"}:
        raise ValueError("MOTOR_REVERSE는 0 또는 1이어야 합니다.")
    max_speed = float(env.get("TRACK_MAX_SPEED", "45"))
    max_step = float(env.get("TRACK_MAX_STEP", "4.5"))
    camera_rotation = int(env.get("CAMERA_ROTATION", "0"))
    if camera_rotation not in {0, 90, 180, 270}:
        raise ValueError("CAMERA_ROTATION은 0, 90, 180, 270도만 지원합니다.")
    camera_fps = int(env.get("CAMERA_FPS", "15"))
    yolo_fps = float(env.get("YOLO_FPS", "10"))
    if not 0 < max_speed <= 180 or not 0 < max_step <= 15:
        raise ValueError("TRACK_MAX_SPEED / TRACK_MAX_STEP 범위 오류")
    if not 1 <= camera_fps <= 30 or not 0 < yolo_fps <= camera_fps:
        raise ValueError("CAMERA_FPS / YOLO_FPS 범위 오류")
    tilt_reverse = env.get("TILT_REVERSE", "0").lower()
    if tilt_reverse not in {"0", "1", "false", "true"}:
        raise ValueError("TILT_REVERSE는 0 또는 1이어야 합니다.")
    tilt_speed = float(env.get("TILT_MAX_SPEED", "20"))
    tilt_step = float(env.get("TILT_MAX_STEP", "2"))
    pan_limit = float(env.get("PAN_LIMIT", "60"))
    tilt_limit = float(env.get("TILT_LIMIT", "20"))
    if not 0 < tilt_speed <= 60 or not 0 < tilt_step <= 5:
        raise ValueError("상하 축 속도/보정 폭 범위 오류")
    if not 0 < pan_limit <= 60 or not 0 < tilt_limit <= 30:
        raise ValueError("시험한 기계 범위 내에서 이동 한계를 지정하세요.")
    return {
        "qwen": [
            str(ROOT / "tools/llama.cpp/build/bin/llama-server"),
            "-m",
            str(ROOT / "models/qwen/Qwen3-1.7B-Q4_K_M.gguf"),
            "--alias",
            "qwen-command",
            "--host",
            "127.0.0.1",
            "--port",
            "8081",
            "-c",
            "2048",
            "-t",
            "3",
            "--parallel",
            "1",
            "--jinja",
        ],
        "camera": [
            PYTHON,
            "-u",
            "-m",
            "robot_vision.camera_server",
            "--yolo",
            "--fps",
            str(camera_fps),
            "--rotation",
            str(camera_rotation),
            "--yolo-fps",
            str(yolo_fps),
            "--oled-video",
            "--oled-fps",
            "3",
        ],
        "tracking": [
            PYTHON,
            "-u",
            "-m",
            "robot_vision.tracking_controller",
            "--hardware",
            "--max-speed",
            str(max_speed),
            "--max-step",
            str(max_step),
            "--tilt-speed",
            str(tilt_speed),
            "--tilt-step",
            str(tilt_step),
            "--pan-limit",
            str(pan_limit),
            "--tilt-limit",
            str(tilt_limit),
        ]
        + (["--tilt-reverse"] if tilt_reverse in {"1", "true"} else [])
        + (["--reverse"] if reverse in {"1", "true"} else []),
        "button": [
            PYTHON,
            "-u",
            "-m",
            "robot_vision.button_voice",
            "--send",
            "--output",
            str(ROOT / "recordings/latest-command.wav"),
            "--seconds",
            str(seconds),
        ],
    }


def check_files():
    files = [
        ".venv/bin/python",
        "robot_vision/camera_server.py",
        "robot_vision/tracking_controller.py",
        "robot_vision/button_voice.py",
        "robot_vision/object_catalog.py",
        "robot_vision/camera_orientation.py",
        "robot_vision/motor.py",
        "robot_vision/microphone.py",
        "robot_vision/command_parser.py",
        "robot_vision/led.py",
        "robot_vision/oled_preview.py",
        "robot_vision/yolo_detector.py",
        "tools/llama.cpp/build/bin/llama-server",
        "tools/whisper.cpp/build/bin/whisper-cli",
        "models/qwen/Qwen3-1.7B-Q4_K_M.gguf",
        "models/whisper/ggml-base.bin",
        "models/yolo/yolo11n_ncnn_model/model.ncnn.param",
        "models/yolo/yolo11n_ncnn_model/model.ncnn.bin",
        "models/yolo/yolo11n_ncnn_model/metadata.yaml",
    ]
    missing = [
        name for name in files if not (ROOT / name).is_file() or not (ROOT / name).stat().st_size
    ]
    if missing:
        raise RuntimeError("필수 파일 없음: " + ", ".join(missing))


def notify(message):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as connection:
        connection.sendto(message.encode("utf-8"), address)


class Stack:
    def __init__(self):
        self.children = {}
        self.stop_event = threading.Event()

    def status(self, text):
        logging.info(text)
        notify("STATUS=" + text)

    def start_child(self, name, command):
        self.status(f"시작 중: {name}")
        env = dict(os.environ, LC_ALL="C.UTF-8", PYTHONUNBUFFERED="1", PYTHONPATH=str(ROOT))
        # 자식이 서비스 준비 완료 메시지를 대신 보내지 않게 한다.
        env.pop("NOTIFY_SOCKET", None)
        # lgpio 알림 FIFO가 프로세스 간 같은 작업 경로에서 충돌하지 않게 분리한다.
        runtime = os.environ.get("RUNTIME_DIRECTORY")
        working = ROOT if not runtime else Path(runtime) / name
        working.mkdir(parents=True, exist_ok=True)
        self.children[name] = subprocess.Popen(
            command,
            cwd=working,
            env=env,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    def check_children(self):
        if self.stop_event.is_set():
            raise InterruptedError("종료 요청")
        for name, child in self.children.items():
            if child.poll() is not None:
                raise RuntimeError(f"{name} 프로세스 종료: {child.returncode}")

    def wait_http(self, name, url, predicate, timeout=90):
        self.status(f"준비 확인 중: {name}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_children()
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    data = json.load(response)
                if predicate(data):
                    return
            except (OSError, ValueError, TypeError, KeyError):
                pass
            self.stop_event.wait(0.3)
        raise RuntimeError(f"{name} 준비 시간 초과: {url}")

    def run(self):
        check_files()
        config = commands()
        # 이미 사용 중인 수동 프로세스와 포트 충돌 시 중복 구동하지 않는다.
        for port in (8080, 8081, 8082):
            with socket.socket() as connection:
                if connection.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError(f"포트 {port} 사용 중. 기존 수동 실행을 종료하세요.")
        self.start_child("qwen", config["qwen"])
        self.start_child("camera", config["camera"])
        self.wait_http(
            "Qwen", "http://127.0.0.1:8081/health", lambda state: state.get("status") == "ok"
        )
        self.wait_http(
            "YOLO",
            "http://127.0.0.1:8080/detections.json",
            lambda state: state.get("status") == "ok",
        )
        self.wait_http(
            "OLED",
            "http://127.0.0.1:8080/oled-status.json",
            lambda state: (
                state.get("initialized") is True
                and (state.get("video") or {}).get("status") == "running"
            ),
        )
        self.start_child("tracking", config["tracking"])
        self.wait_http(
            "모터 연결부",
            "http://127.0.0.1:8082/status",
            lambda state: state.get("mode") == "hardware" and state.get("target") is None,
            timeout=15,
        )
        self.start_child("button", config["button"])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            self.check_children()
            self.stop_event.wait(0.2)
        self.status("준비 완료: 버튼 한 번 → 녹음 → 음성 명령 → 물체 추적")
        notify("READY=1")
        while not self.stop_event.wait(1):
            self.check_children()

    def close(self):
        notify("STOPPING=1")
        self.stop_event.set()
        # 버튼을 먼저 멈춰 새 명령 유입을 막고, 이후 모터·카메라·모델을 종료한다.
        for name in reversed(list(self.children)):
            child = self.children[name]
            if child.poll() is not None:
                continue
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="필수 파일과 설정만 검사")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [stack] %(message)s")
    if args.check:
        check_files()
        commands()
        print("필수 파일·실행 설정 확인 완료")
        return 0
    stack = Stack()
    signal.signal(signal.SIGTERM, lambda *_: stack.stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stack.stop_event.set())
    try:
        stack.run()
        return 0
    except InterruptedError:
        return 0
    except Exception:
        logging.exception("서비스 실행 실패")
        return 1
    finally:
        stack.close()


if __name__ == "__main__":
    raise SystemExit(main())
