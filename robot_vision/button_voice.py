"""물리16번(GPIO23) 버튼 한 번 누름 → 고정 시간 녹음 → 다음 누름 대기.

GPIO와 GND 사이의 normally-open 버튼을 내부 풀업으로 읽는다.
LED와 OLED는 아직 제어하지 않는다. --send에서만 추적 명령을 전달한다.
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

from .paths import PROJECT_ROOT as ROOT

BUTTON_GPIO = 23  # BCM GPIO23 = 물리 핀 16번


class PressGate:
    """눌림당 한 번만 요청하고, 처리 중 입력을 나중에 재실행하지 않는다."""

    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.armed = False
        self.busy = False

    def pressed(self):
        with self.lock:
            if not self.armed or self.busy:
                return False
            self.armed = False
            self.busy = True
            self.event.set()
            return True

    def released(self):
        with self.lock:
            if not self.busy:
                self.armed = True

    def finish(self, released):
        with self.lock:
            self.event.clear()
            self.busy = False
            self.armed = released


def microphone_command(args):
    command = [
        sys.executable,
        "-m",
        "robot_vision.microphone",
        "--device",
        args.device,
        "--seconds",
        str(args.seconds),
        "--channel",
        args.channel,
        "--gain",
        str(args.gain),
    ]
    if args.record_only:
        command.append("--record-only")
    if args.parse:
        command.append("--parse")
    if args.send:
        command.append("--send")
    if args.output is not None:
        command.extend(["--output", str(args.output.resolve())])
    # --ptt는 넣지 않는다. 버튼은 부모가 소유하고, 자식은 고정 시간 녹음한다.
    return command


def check_ready(args):
    from .microphone import ROOT as MIC_ROOT
    from .microphone import capture_devices

    listing, available = capture_devices()
    if not available:
        raise RuntimeError("녹음 장치가 없습니다. arecord -l을 확인하세요.\n" + listing)
    if not args.record_only:
        for path in [
            MIC_ROOT / "tools/whisper.cpp/build/bin/whisper-cli",
            MIC_ROOT / "models/whisper/ggml-base.bin",
        ]:
            if not path.is_file():
                raise RuntimeError(f"Whisper 준비 파일 없음: {path}")
    urls = []
    if args.parse or args.send:
        urls.append(("Qwen", "http://127.0.0.1:8081/health"))
    if args.send:
        urls.append(("추적 연결부", "http://127.0.0.1:8082/status"))
    for name, url in urls:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                response.read(65536)
        except OSError as error:
            raise RuntimeError(f"{name} 서버에 연결할 수 없습니다: {url}") from error


def stop_child(process):
    if process is None or process.poll() is not None:
        return
    # 자식의 arecord/Whisper도 같은 프로세스 그룹으로 함께 종료한다.
    try:
        os.killpg(process.pid, signal.SIGINT)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="hw:CARD=SPH0645,DEV=0")
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--channel", choices=["left", "right"], default="left")
    parser.add_argument("--gain", type=float, default=1)
    parser.add_argument("--output", type=Path, help="녹음 WAV 저장 경로. 매번 최신 녹음으로 덮어씀")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--button-test", action="store_true", help="버튼 입력만 확인, 녹음 없음")
    modes.add_argument("--record-only", action="store_true", help="버튼으로 녹음만 실행")
    modes.add_argument("--parse", action="store_true", help="음성 인식 후 Qwen 명령 JSON 확인")
    modes.add_argument("--send", action="store_true", help="음성 인식·Qwen 해석 후 추적 목표 전달")
    args = parser.parse_args()
    if not 0.3 <= args.seconds <= 15:
        parser.error("--seconds는 0.3~15초 범위입니다.")
    if not 0 < args.gain <= 16:
        parser.error("--gain은 0 초과 16 이하여야 합니다.")

    if not args.button_test:
        check_ready(args)

    from gpiozero import Button
    from gpiozero.pins.lgpio import LGPIOFactory

    factory = None
    button = None
    child = None
    gate = PressGate()
    try:
        factory = LGPIOFactory()
        button = Button(BUTTON_GPIO, pull_up=True, bounce_time=0.05, pin_factory=factory)
        button.when_pressed = gate.pressed
        button.when_released = gate.released
        if not button.is_pressed:
            gate.released()

        def interrupt(*_):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupt)
        print("버튼: 물리16번 / GPIO23, 내부 풀업, 누르면 LOW", file=sys.stderr, flush=True)
        if button.is_pressed:
            print("현재 눌림 상태입니다. 먼저 버튼을 놓아 주세요.", file=sys.stderr, flush=True)
        if args.button_test:
            print("버튼 입력만 검사합니다. 녹음·모터 동작 없음.", file=sys.stderr, flush=True)
        else:
            print(
                f"한 번 누르면 {args.seconds:g}초 녹음합니다. 누르고 있을 필요 없습니다.",
                file=sys.stderr,
                flush=True,
            )
            if args.send:
                print(
                    "--send 활성: 연결부가 hardware 모드이면 모터가 움직일 수 있습니다.",
                    file=sys.stderr,
                    flush=True,
                )
        count = 0
        print("버튼 입력 대기 / 종료: Ctrl+C", file=sys.stderr, flush=True)
        while True:
            if not gate.event.wait(timeout=0.2):
                continue
            try:
                count += 1
                print(f"버튼 누름 #{count}", file=sys.stderr, flush=True)
                if not args.button_test:
                    child = subprocess.Popen(
                        microphone_command(args),
                        cwd=ROOT,
                        env=dict(os.environ, LC_ALL="C.UTF-8"),
                        start_new_session=True,
                    )
                    code = child.wait()
                    child = None
                    if code != 0:
                        print(
                            f"음성 처리 실패(종료 코드 {code}). 다음 버튼 입력을 기다립니다.",
                            file=sys.stderr,
                            flush=True,
                        )
            finally:
                gate.finish(released=not button.is_pressed)
            print(
                "버튼 입력 대기 / 처리 중 누른 입력은 저장하지 않습니다.",
                file=sys.stderr,
                flush=True,
            )
    except KeyboardInterrupt:
        pass
    finally:
        if button is not None:
            button.when_pressed = None
            button.when_released = None
        stop_child(child)
        if button is not None:
            button.close()
        if factory is not None:
            factory.close()
        print("버튼 음성 입력 종료", file=sys.stderr, flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"버튼 음성 입력 실패: {error}", file=sys.stderr)
        raise SystemExit(1)
