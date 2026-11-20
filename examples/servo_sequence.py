#!/usr/bin/env python3
"""물리 33번 MG996R 단독 시험. 각도는 미보정 PWM 환산값."""

import argparse
import math
import os
import signal
import subprocess
import time
from pathlib import Path

CHIP = Path("/sys/class/pwm/pwmchip0")
PWM = CHIP / "pwm1"
PERIOD_NS = 20_000_000


def pulse_ns(angle):
    if not math.isfinite(angle) or not -60 <= angle <= 60:
        raise ValueError("시험 목표는 중앙 기준 -60~+60도만 허용합니다.")
    return round(1_500_000 + angle / 90 * 500_000)


def plan():
    angle = 0
    targets = []
    for direction, count in ((1, 4), (-1, 8), (1, 4)):
        for _ in range(count):
            angle += direction * 15
            targets.append(angle)
    return targets


def write(name, value):
    (PWM / name).write_text(str(value))


def ramp(start, target, speed):
    duration = abs(target - start) / speed
    began = time.monotonic()
    while True:
        fraction = min(1.0, (time.monotonic() - began) / duration)
        # 출발과 정지 시 급격한 변화를 줄이는 보간
        fraction = fraction * fraction * (3 - 2 * fraction)
        write("duty_cycle", pulse_ns(start + (target - start) * fraction))
        if time.monotonic() - began >= duration:
            write("duty_cycle", pulse_ns(target))
            return
        time.sleep(0.02)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--speed",
        type=float,
        default=10,
        help="이동 시간 계산용 평균 속도. 기본 10, 최대 20(환산 도/초)",
    )
    parser.add_argument("--pause", type=float, default=0.5, help="각 15도 이동 후 대기 시간(초)")
    parser.add_argument("--dry-run", action="store_true", help="모터 출력 없이 순서만 확인")
    args = parser.parse_args()
    if not math.isfinite(args.speed) or not 0 < args.speed <= 20:
        parser.error("--speed는 0 초과 20 이하입니다.")
    if not math.isfinite(args.pause) or not 0 <= args.pause <= 10:
        parser.error("--pause는 0~10초입니다.")
    targets = plan()
    print("각도는 미보정 환산값이며, 실제 각도와 다를 수 있습니다.", flush=True)
    print("순서: 0 → " + " → ".join(map(str, targets)), flush=True)
    if args.dry_run:
        return
    if os.geteuid() != 0:
        parser.error("하드웨어 시험은 sudo python3 examples/servo_sequence.py 로 실행하세요.")
    if subprocess.run(["systemctl", "is-active", "--quiet", "pjt-robot.service"]).returncode == 0:
        raise RuntimeError("추적 서비스를 먼저 정지하세요: sudo systemctl stop pjt-robot.service")
    if not CHIP.exists():
        raise RuntimeError("PWM 컨트롤러가 없습니다. 부팅 PWM 설정을 확인하세요.")
    for channel in ("pwm0", "pwm1"):
        enabled = CHIP / channel / "enable"
        if enabled.exists() and enabled.read_text().strip() == "1":
            raise RuntimeError(f"{channel}이 이미 사용 중입니다. 시험을 중단합니다.")
    if not PWM.exists():
        (CHIP / "export").write_text("1")
        deadline = time.monotonic() + 2
        while not (PWM / "enable").exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("PWM 채널 생성 시간 초과")
            time.sleep(0.05)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        write("duty_cycle", 0)
        write("period", PERIOD_NS)
        write("polarity", "normal")
        subprocess.run(["pinctrl", "set", "13", "a0"], check=True)
        print("3초 후 중앙으로 이동합니다. 최초 중앙 이동의 속도는 보장되지 않습니다.", flush=True)
        time.sleep(3)
        write("duty_cycle", pulse_ns(0))
        write("enable", 1)
        time.sleep(2)
        current = 0
        for index, target in enumerate(targets, 1):
            print(f"[{index:02d}/16] {current:+d} → {target:+d}도", flush=True)
            ramp(current, target, args.speed)
            current = target
            time.sleep(args.pause)
        print("중앙 복귀 완료. 2초 후 출력을 해제합니다.", flush=True)
        time.sleep(2)
    except KeyboardInterrupt:
        print("\n시험 중단", flush=True)
    finally:
        try:
            write("duty_cycle", 0)
        finally:
            write("enable", 0)
        print("33번 PWM 해제 완료. 32번 SG90은 출력하지 않았습니다.", flush=True)


if __name__ == "__main__":
    main()
