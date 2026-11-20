"""하드웨어 PWM 출력과 영상 중심 오차에 따른 두 축 보정."""

import math
import time
from pathlib import Path

# 물리 32번(GPIO12) → pwmchip0의 채널 0
PWM_PATH = Path("/sys/class/pwm/pwmchip0/pwm0")


class HardwareServo:
    def __init__(self, channel=0):
        if channel not in (0, 1):
            raise ValueError("PWM 채널은 0 또는 1입니다.")
        self.path = PWM_PATH.parent / f"pwm{channel}"
        chip = self.path.parent

        if not chip.exists():
            raise RuntimeError("하드웨어 PWM 설정을 확인하세요.")

        if not self.path.exists():
            (chip / "export").write_text(str(channel))
            time.sleep(0.2)

        if (self.path / "enable").read_text().strip() == "1":
            raise RuntimeError("PWM이 이미 실행 중입니다.")

        # 출력은 켜지 않고 주기와 극성만 준비
        self.write("duty_cycle", 0)
        self.write("period", 20_000_000)  # 50Hz
        self.write("polarity", "normal")

    def write(self, name, value):
        (self.path / name).write_text(str(value))

    def move(self, angle):
        # PWM 환산 각도의 입력 범위를 제한한다.
        if not -90 <= angle <= 90:
            raise ValueError("각도는 -90~90도 범위만 허용합니다.")

        # 펄스 폭과 환산 각도:
        # -90도 → 1ms, 0도 → 1.5ms, +90도 → 2ms
        pulse_ns = round(1_500_000 + angle / 90 * 500_000)

        self.write("duty_cycle", pulse_ns)
        self.write("enable", 1)

    def release(self):
        # 펄스를 없앤 다음 출력을 비활성화
        self.write("duty_cycle", 0)
        self.write("enable", 0)


class Tracker:
    def __init__(
        self,
        servo,
        reverse=False,
        indicators=None,
        max_speed=60.0,
        max_step=6.0,
        axis="x",
        angle_limit=90.0,
    ):
        self.servo = servo
        self.reverse = reverse
        self.indicators = indicators

        self.angle = 0.0
        self.initialized = False
        self.last_frame_id = -1
        self.last_timestamp = None

        self.deadband = 0.025  # 박스 중심 오차: 가로 480px에서 약 6px
        self.min_speed = 5.0  # 허용 구간 밖에서 미세 보정이 멎지 않게 한다
        self.max_speed = self.number(max_speed)  # 목표 각도 보정 속도: 도/초
        self.max_step = self.number(max_step)  # 한 번의 보정 폭 상한
        if not 0 < self.max_speed <= 180 or not 0 < self.max_step <= 15:
            raise ValueError("속도는 0~180도/초, 보정 폭은 0~15도 범위(0 제외)입니다.")
        self.max_age = 0.5  # 0.5초보다 오래된 영상은 무시
        self.angle_limit = self.number(angle_limit)
        if axis not in {"x", "y"} or not 0 < self.angle_limit <= 90:
            raise ValueError("축 또는 이동 한계 오류")
        self.axis = axis

    @staticmethod
    def number(value):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("좌표와 시간은 유한한 숫자여야 합니다.")
        return float(value)

    def update(self, data):
        if not isinstance(data, dict):
            raise ValueError("JSON 객체가 필요합니다.")

        frame_id = data.get("frame_id")
        if type(frame_id) is not int or frame_id < 0:
            raise ValueError("frame_id는 0 이상의 정수여야 합니다.")

        timestamp = self.number(data.get("timestamp"))
        width = self.number(data.get("frame_width"))
        height = self.number(data.get("frame_height"))

        if width <= 0 or height <= 0:
            raise ValueError("화면 크기는 양수여야 합니다.")

        if "bbox" not in data:
            raise ValueError("bbox가 필요합니다.")

        bbox = data["bbox"]
        error = None

        if bbox is not None:
            if not isinstance(bbox, list) or len(bbox) != 4:
                raise ValueError("bbox는 [x1, y1, x2, y2]입니다.")

            x1, y1, x2, y2 = map(self.number, bbox)

            if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                raise ValueError("bbox 범위가 잘못됐습니다.")

            center = (x1 + x2) / 2 if self.axis == "x" else (y1 + y2) / 2
            dimension = width if self.axis == "x" else height
            error = (center - dimension / 2) / (dimension / 2)

        if frame_id <= self.last_frame_id:
            return self.result("duplicate_or_old")

        age = time.monotonic() - timestamp
        if age < -0.1 or age > self.max_age:
            return self.result("stale")

        if self.last_timestamp is not None and timestamp <= self.last_timestamp:
            return self.result("old_timestamp")

        # 결과 처리 간격 대신 영상 촬영 시각의 간격을 사용한다.
        dt = 0.0 if self.last_timestamp is None else min(timestamp - self.last_timestamp, 0.2)

        self.last_frame_id = frame_id
        self.last_timestamp = timestamp

        if bbox is None:
            # 목표 위치는 유지하되 재검출 시 큰 보정을 방지
            self.last_timestamp = None
            return self.result("target_lost")

        if not self.initialized:
            self.servo.move(0)
            self.initialized = True
            return self.result("initialized", error)

        if abs(error) <= self.deadband:
            return self.result("centered", error)

        # 오차가 클수록 빠르게, 중앙 부근에서는 천천히 보정
        ratio = (abs(error) - self.deadband) / (1 - self.deadband)
        step = math.copysign(
            min(
                max(min(self.min_speed, self.max_speed), self.max_speed * ratio) * dt, self.max_step
            ),
            error,
        )

        if self.reverse:
            step = -step

        target = max(
            -self.angle_limit,
            min(self.angle_limit, self.angle + step),
        )

        delta = 0.0
        if abs(target - self.angle) > 0.001:
            self.servo.move(target)
            delta = target - self.angle
            self.angle = target

        status = "angle_limit" if abs(self.angle) >= self.angle_limit else "tracking"
        return self.result(status, error, delta)

    def result(self, status, error=None, delta=0.0):
        command = None
        if abs(delta) > 0.001:
            logical_delta = -delta if self.reverse else delta
            direction = (
                ("RIGHT" if logical_delta > 0 else "LEFT")
                if self.axis == "x"
                else ("DOWN" if logical_delta > 0 else "UP")
            )
            command = {
                "axis": "pan" if self.axis == "x" else "tilt",
                "motor": "MG996R" if self.axis == "x" else "SG90",
                "direction": direction,
                "target_angle": round(self.angle, 2),
                "issued_at_ns": time.monotonic_ns(),
            }
        if self.indicators is not None and status not in {"duplicate_or_old", "old_timestamp"}:
            logical_angle = -self.angle if self.reverse else self.angle
            logical_delta = -delta if self.reverse else delta
            if (
                status in {"angle_limit", "tracking", "centered"}
                and abs(self.angle) >= self.angle_limit
            ):
                led_state = "right_limit" if logical_angle > 0 else "left_limit"
            elif status == "tracking" and abs(logical_delta) > 0.001:
                led_state = "right" if logical_delta > 0 else "left"
            else:
                led_state = "stopped"
            self.indicators.set_state(led_state)
        return {
            "ok": True,
            "status": status,
            "target_angle": round(self.angle, 2),
            "command": command,
            "error_" + self.axis: None if error is None else round(error, 3),
        }


class PanTiltTracker:
    """동일한 최신 프레임으로 두 축을 보정한다. LED는 수평 축 상태를 표시한다."""

    def __init__(
        self,
        pan_servo,
        tilt_servo,
        reverse=False,
        indicators=None,
        max_speed=30,
        max_step=3,
        tilt_reverse=False,
        tilt_speed=10,
        tilt_step=1,
        pan_limit=60,
        tilt_limit=20,
    ):
        self.pan = Tracker(
            pan_servo, reverse, indicators, max_speed, max_step, axis="x", angle_limit=pan_limit
        )
        self.tilt = Tracker(
            tilt_servo, tilt_reverse, None, tilt_speed, tilt_step, axis="y", angle_limit=tilt_limit
        )
        self.tilt.deadband = 0.035  # 세로 640px에서 약 11px
        self.tilt.min_speed = 3.5

    @property
    def angle(self):
        return self.pan.angle

    @property
    def max_speed(self):
        return self.pan.max_speed

    @property
    def max_step(self):
        return self.pan.max_step

    @property
    def max_age(self):
        return min(self.pan.max_age, self.tilt.max_age)

    @property
    def last_timestamp(self):
        return self.pan.last_timestamp

    @last_timestamp.setter
    def last_timestamp(self, value):
        self.pan.last_timestamp = self.tilt.last_timestamp = value

    def axes(self):
        return {
            name: {
                "angle": round(axis.angle, 2),
                "limit": axis.angle_limit,
                "reverse": axis.reverse,
                "max_speed": axis.max_speed,
                "max_step": axis.max_step,
                "deadband": axis.deadband,
                "min_speed": axis.min_speed,
            }
            for name, axis in (("pan", self.pan), ("tilt", self.tilt))
        }

    def update(self, data):
        pan = self.pan.update(data)
        tilt = self.tilt.update(data)
        commands = [r["command"] for r in (pan, tilt) if r.get("command") is not None]
        latest = max(commands, key=lambda c: c["issued_at_ns"]) if commands else None
        statuses = {pan["status"], tilt["status"]}
        status = pan["status"]
        if "tracking" in statuses:
            status = "tracking"
        elif "angle_limit" in statuses:
            status = "angle_limit"
        return {
            **pan,
            "status": status,
            "latest_command": latest,
            "target_angles": {"pan": round(self.pan.angle, 2), "tilt": round(self.tilt.angle, 2)},
            "error_y": tilt.get("error_y"),
            "axes": {"pan": pan, "tilt": tilt},
        }
