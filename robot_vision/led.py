"""방향 LED: 오른쪽 물리11번(GPIO17), 왼쪽 물리13번(GPIO27)."""

import logging
import threading
import time

RIGHT_GPIO = 17
LEFT_GPIO = 27
STATES = {"stopped", "right", "left", "right_limit", "left_limit"}


class LEDState:
    """GPIO 없이 검사할 수 있는 점등 상태·점멸 타이머."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.state = "stopped"
        self.changed_at = clock()
        self.expires_at = None

    def set_state(self, state):
        if state not in STATES:
            raise ValueError(f"지원하지 않는 LED 상태: {state}")
        with self.lock:
            now = self.clock()
            if state != self.state:
                self.state = state
                self.changed_at = now
            self.expires_at = now + 0.5 if state in {"right", "left"} else None

    def snapshot(self):
        with self.lock:
            now = self.clock()
            if self.expires_at is not None and now >= self.expires_at:
                self.state = "stopped"
                self.changed_at = now
                self.expires_at = None
            blink_on = int(max(0, now - self.changed_at) // 1.0) % 2 == 0
            right = self.state in {"right", "left_limit"}
            left = self.state in {"left", "right_limit"}
            if self.state == "right_limit":
                right = blink_on
            elif self.state == "left_limit":
                left = blink_on
            return {"state": self.state, "right": right, "left": left}


class MotionLEDs:
    def __init__(self):
        from gpiozero import LED
        from gpiozero.pins.lgpio import LGPIOFactory

        self.pattern = LEDState()
        self.stop_event = threading.Event()
        self.factory = LGPIOFactory()
        self.right = self.left = None
        try:
            self.right = LED(RIGHT_GPIO, initial_value=False, pin_factory=self.factory)
            self.left = LED(LEFT_GPIO, initial_value=False, pin_factory=self.factory)
        except Exception:
            if self.right is not None:
                self.right.close()
            self.factory.close()
            raise
        self.thread = threading.Thread(target=self.run, name="motion-leds", daemon=True)
        self.thread.start()

    def set_state(self, state):
        self.pattern.set_state(state)

    def snapshot(self):
        return self.pattern.snapshot()

    def run(self):
        previous = None
        try:
            while not self.stop_event.is_set():
                state = self.pattern.snapshot()
                values = (state["right"], state["left"])
                if values != previous:
                    self.right.value, self.left.value = values
                    previous = values
                self.stop_event.wait(0.025)
        except Exception:
            logging.exception("방향 LED 출력 실패")
        finally:
            self.right.off()
            self.left.off()

    def close(self):
        self.stop_event.set()
        self.thread.join()
        try:
            self.right.close()
            self.left.close()
        finally:
            self.factory.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="모터 없이 LED 패턴만 순서대로 시험")
    args = parser.parse_args()
    if not args.test:
        parser.error("단독 시험은 --test를 지정하세요.")
    leds = MotionLEDs()
    try:
        for state, seconds in [
            ("stopped", 1),
            ("right", 2),
            ("left", 2),
            ("stopped", 1),
            ("right_limit", 5),
            ("left_limit", 5),
            ("stopped", 1),
        ]:
            print(f"LED 시험: {state}, {seconds}초", flush=True)
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                leds.set_state(state)
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        leds.close()


if __name__ == "__main__":
    main()
