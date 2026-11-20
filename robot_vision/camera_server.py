"""카메라 JPEG를 웹 스트림, YOLO 검출기, OLED 미리보기에 공유한다."""

import argparse
import io
import json
import logging
import signal
import socketserver
import time
from http import server
from pathlib import Path
from threading import Condition, Lock
from urllib.parse import parse_qs, urlsplit

from gpiozero import LED
from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.outputs import FileOutput

from .camera_orientation import rotate_jpeg
from .paths import PROJECT_ROOT as ROOT

STREAM_PORT = 8080
STREAM_PATH = "/stream.mjpg"


STREAM_WIDTH = 640
STREAM_HEIGHT = 480
STREAM_FPS = 15


GPIO_BLINK_PATH = "/gpio-blink"
GPIO_BLINK_COUNT = 3
GPIO_BLINK_INTERVAL_SECONDS = 0.3


OLED_DISPLAY_PATH = "/oled-display"

OLED_MAX_TEXT_LENGTH = 16


OLED_WIDTH = 128
OLED_HEIGHT = 64

OLED_SPI_PORT = 0
OLED_SPI_DEVICE = 0


OLED_GPIO_DC = 25
OLED_GPIO_RST = 22


OLED_ROTATE = 2


OLED_START_TEXT = "start"


OLED_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)

OLED_MAX_FONT_SIZE = 40
OLED_MARGIN = 4

logging.basicConfig(level=logging.INFO)


def _interrupt_server(*_):
    raise KeyboardInterrupt


_led_cache = {}
_gpio_lock = Lock()


def _get_led(pin):
    if pin not in _led_cache:
        _led_cache[pin] = LED(pin)
    return _led_cache[pin]


_oled_device = None

_oled_init_failed = False
_oled_lock = Lock()
oled_video_worker = None
cli = output = yolo_worker = None


def get_oled_device():
    """SPI OLED 장치를 최초 요청 시 한 번만 초기화한다(실패하면 None)."""
    global _oled_device, _oled_init_failed
    if _oled_device is None and not _oled_init_failed:
        try:
            from luma.core.interface.serial import spi
            from luma.oled.device import sh1106, ssd1306

            serial = spi(
                port=OLED_SPI_PORT,
                device=OLED_SPI_DEVICE,
                gpio_DC=OLED_GPIO_DC,
                gpio_RST=OLED_GPIO_RST,
            )
            driver = ssd1306 if cli.oled_driver == "ssd1306" else sh1106
            _oled_device = driver(
                serial, width=OLED_WIDTH, height=OLED_HEIGHT, rotate=cli.oled_rotate
            )
            logging.info(
                "OLED: 초기화 완료 (%s, %dx%d, rotate=%d)",
                cli.oled_driver,
                OLED_WIDTH,
                OLED_HEIGHT,
                cli.oled_rotate,
            )
        except Exception:
            logging.exception("OLED: 초기화 실패 - OLED 없이 계속 진행한다")
            _oled_init_failed = True
    return _oled_device


def _load_oled_font(size):
    """OLED에 쓸 TrueType 폰트를 찾는다. 하나도 없으면 None(PIL 기본 폰트)."""
    from PIL import ImageFont

    for candidate in OLED_FONT_CANDIDATES:
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return None


def _build_text_image(text):
    """글자를 화면 폭·높이에 들어가는 가장 큰 크기로 가운데에 그린다."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT), 0)
    draw = ImageDraw.Draw(image)
    font = None
    for size in range(OLED_MAX_FONT_SIZE, 7, -2):
        candidate = _load_oled_font(size)
        if candidate is None:
            break
        bbox = draw.textbbox((0, 0), text, font=candidate)
        if (
            bbox[2] - bbox[0] <= OLED_WIDTH - OLED_MARGIN * 2
            and bbox[3] - bbox[1] <= OLED_HEIGHT - OLED_MARGIN * 2
        ):
            font = candidate
            break
    if font is None:
        logging.info("OLED: TrueType 폰트를 찾지 못해 기본 폰트를 쓴다(글씨가 작게 보인다)")
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), text, font=font)
    x = (OLED_WIDTH - (bbox[2] - bbox[0])) // 2 - bbox[0]
    y = (OLED_HEIGHT - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=1)
    return image


def show_oled_text(text):
    """OLED에 글자를 표시한다. 결과를 문자열로 돌려주고, 어떤 실패도 예외로 올리지 않는다."""
    device = get_oled_device()
    if device is None:
        return "unavailable"
    if oled_video_worker is not None:
        oled_video_worker.pause(3)
    try:
        with _oled_lock:
            device.display(_build_text_image(text))
    except Exception:
        logging.exception("OLED: 화면 갱신 실패 (text=%s)", text)
        return "error"
    logging.info("OLED: %s 표시 완료", text)
    return "shown"


class StreamingOutput(io.BufferedIOBase):
    """JpegEncoder가 인코딩한 프레임을 조건 변수로 넘겨받아 최신 프레임만 보관한다.

    여러 클라이언트(스트리밍 요청 스레드)가 같은 최신 프레임을 동시에 읽어야 하므로,
    프레임을 큐에 쌓지 않고 하나만 유지한 채 Condition으로 새 프레임 도착을 알린다.
    """

    def __init__(self, rotation=0):
        self.rotation = rotation
        self.frame = None
        self.frame_id = -1
        self.timestamp = None
        self.condition = Condition()

    def write(self, buf):
        # 소비자들이 같은 방향과 좌표계를 사용하도록 공유 전에 회전한다.
        timestamp = time.monotonic()
        frame = rotate_jpeg(buf, self.rotation)
        with self.condition:
            self.frame = frame
            self.frame_id += 1
            self.timestamp = timestamp
            self.condition.notify_all()
        return len(buf)


class StreamingHandler(server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == STREAM_PATH:
            self._handle_stream()
        elif parsed.path == "/yolo.mjpg":
            if yolo_worker is None:
                self._send_json({"error": "yolo_disabled"}, status=503)
            else:
                self._handle_stream(yolo_worker)
        elif parsed.path == "/detections.json":
            if yolo_worker is None:
                self._send_json({"error": "yolo_disabled"}, status=503)
            else:
                self._send_json(yolo_worker.snapshot())
        elif parsed.path == "/oled-status.json":
            self._send_json(
                {
                    "initialized": _oled_device is not None,
                    "initialization_failed": _oled_init_failed,
                    "driver": cli.oled_driver,
                    "width": OLED_WIDTH,
                    "height": OLED_HEIGHT,
                    "rotate": cli.oled_rotate,
                    "video_enabled": cli.oled_video,
                    "video": None if oled_video_worker is None else oled_video_worker.snapshot(),
                }
            )
        elif parsed.path == GPIO_BLINK_PATH:
            self._handle_gpio_blink(parse_qs(parsed.query))
        elif parsed.path == OLED_DISPLAY_PATH:
            self._handle_oled_display(parse_qs(parsed.query))
        else:
            self.send_error(404)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_stream(self, source=None):
        """각 클라이언트에 최신 JPEG만 전송한다."""
        if source is None:
            source = output
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")

        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
        self.end_headers()
        try:
            last_frame_id = -1
            while True:
                with source.condition:
                    ready = source.condition.wait_for(
                        lambda: source.frame is not None and source.frame_id != last_frame_id,
                        timeout=5,
                    )
                    if not ready:
                        continue
                    frame = source.frame
                    last_frame_id = source.frame_id
                self.wfile.write(b"--FRAME\r\n")
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            logging.info("Client disconnected: %s", self.client_address)

    def _handle_gpio_blink(self, query):
        """기존 대시보드에서 요청한 GPIO를 세 번 점멸한다."""
        pin_values = query.get("pin")
        if not pin_values or not pin_values[0].isdigit():
            self._send_json({"error": "invalid_request"}, status=400)
            return
        pin = int(pin_values[0])
        logging.info("GPIO blink requested: pin=%d", pin)

        with _gpio_lock:
            led = _get_led(pin)
            for _ in range(GPIO_BLINK_COUNT):
                led.on()
                time.sleep(GPIO_BLINK_INTERVAL_SECONDS)
                led.off()
                time.sleep(GPIO_BLINK_INTERVAL_SECONDS)
        logging.info("GPIO blink done: pin=%d", pin)
        self._send_json({"ok": True})

    def _handle_oled_display(self, query):
        """대시보드의 텍스트 요청을 잠시 표시한 뒤 영상으로 돌아간다."""
        text_values = query.get("text")
        text = text_values[0].strip() if text_values else ""
        if not text or len(text) > OLED_MAX_TEXT_LENGTH:
            self._send_json({"error": "invalid_request"}, status=400)
            return
        self._send_json({"ok": True, "oled": show_oled_text(text)})

    def log_message(self, format, *args):
        pass


class StreamingServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv=None):
    global cli, output, yolo_worker, oled_video_worker
    signal.signal(signal.SIGTERM, _interrupt_server)
    parser = argparse.ArgumentParser(description="카메라 스트림·YOLO·OLED 서버")
    parser.add_argument(
        "--fps",
        type=int,
        default=STREAM_FPS,
        help=f"카메라 FPS (기본 {STREAM_FPS}). 부하가 크면 해상도 대신 이 값을 낮춘다",
    )
    parser.add_argument(
        "--rotation",
        type=int,
        choices=[0, 90, 180, 270],
        default=0,
        help="카메라 영상 시계 방향 회전 각도",
    )
    parser.add_argument("--yolo", action="store_true", help="NCNN YOLO 검출 활성화")
    parser.add_argument("--yolo-fps", type=float, default=5, help="검출 처리 빈도 상한 (기본 5)")
    parser.add_argument("--confidence", type=float, default=0.4, help="검출 신뢰도 하한 (기본 0.4)")
    parser.add_argument("--yolo-model", type=Path, default=ROOT / "models/yolo/yolo11n_ncnn_model")
    parser.add_argument(
        "--oled-video", action="store_true", help="OLED에 단색 카메라 미리보기 표시"
    )
    parser.add_argument("--oled-fps", type=float, default=3, help="OLED 갱신 빈도 상한 (기본 3)")
    parser.add_argument("--oled-driver", choices=["ssd1306", "sh1106"], default="ssd1306")
    parser.add_argument("--oled-rotate", type=int, choices=[0, 2], default=OLED_ROTATE)
    parser.add_argument(
        "--oled-no-dither", action="store_true", help="점무늬 명암 표현 대신 단순 흑백 사용"
    )
    cli = parser.parse_args(argv)
    if not 0 < cli.oled_fps <= 10:
        parser.error("--oled-fps는 0 초과 10 이하여야 합니다.")
    if not 0 < cli.yolo_fps <= 30:
        parser.error("--yolo-fps는 0 초과 30 이하여야 합니다.")
    if not 0 < cli.confidence < 1:
        parser.error("--confidence는 0 초과 1 미만이어야 합니다.")
    stream_fps = max(1, min(cli.fps, 30))

    output = StreamingOutput(rotation=cli.rotation)
    yolo_worker = None
    if cli.yolo:
        from .yolo_detector import YoloWorker

        yolo_worker = YoloWorker(output, cli.yolo_model, cli.yolo_fps, cli.confidence)

    picam2 = Picamera2()
    video_config = picam2.create_video_configuration(
        main={"size": (STREAM_WIDTH, STREAM_HEIGHT)},
        controls={
            "FrameDurationLimits": (int(1_000_000 / stream_fps), int(1_000_000 / stream_fps))
        },
    )
    picam2.configure(video_config)

    picam2.start_recording(JpegEncoder(), FileOutput(output))
    if yolo_worker is not None:
        yolo_worker.start()
        logging.info("YOLO 활성화: /yolo.mjpg, /detections.json (최대 %.1f FPS)", cli.yolo_fps)

    logging.info("OLED: 기동 화면 표시 -> %s", show_oled_text(OLED_START_TEXT))
    if cli.oled_video and _oled_device is not None:
        from .oled_preview import MotorStatusReader, OLEDVideoWorker

        oled_video_worker = OLEDVideoWorker(
            output,
            _oled_device,
            _oled_lock,
            fps=cli.oled_fps,
            dither=not cli.oled_no_dither,
            motor_status=MotorStatusReader(),
        )
        oled_video_worker.start()
        logging.info(
            "OLED 영상 활성화: %dx%d 단색, 최대 %.1f FPS", OLED_WIDTH, OLED_HEIGHT, cli.oled_fps
        )

    try:
        logging.info(
            "Streaming on http://0.0.0.0:%d%s (%dx%d, %dfps)",
            STREAM_PORT,
            STREAM_PATH,
            STREAM_HEIGHT if cli.rotation in (90, 270) else STREAM_WIDTH,
            STREAM_WIDTH if cli.rotation in (90, 270) else STREAM_HEIGHT,
            stream_fps,
        )
        StreamingServer(("", STREAM_PORT), StreamingHandler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if oled_video_worker is not None:
            oled_video_worker.close()
        if yolo_worker is not None:
            yolo_worker.close()
        picam2.stop_recording()

        if _oled_device is not None:
            try:
                _oled_device.clear()
            except Exception:
                logging.exception("OLED: 종료 시 화면 정리 실패")
        for led in _led_cache.values():
            led.close()


if __name__ == "__main__":
    main()
