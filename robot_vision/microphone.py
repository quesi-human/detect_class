"""SPH0645 녹음 → 16kHz WAV → 로컬 Whisper → 선택적으로 Qwen 명령 해석.

기본 실행은 한 번만 녹음한다. 모터나 추적 목표를 변경하지 않는다.
--send를 지정하면 Qwen 명령을 추적 연결부에 전달하며, 연결부가 hardware 모드이면 모터가 움직인다.
SEL 기본 LOW인 보드를 가정하며, --channel right로 채널을 바꿀 수 있다.
"""

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np

from .paths import PROJECT_ROOT as ROOT

ENV = dict(os.environ, LC_ALL="C.UTF-8")


def capture_devices():
    result = subprocess.run(
        ["arecord", "-l"],
        capture_output=True,
        text=True,
        env=ENV,
        check=False,
    )
    text = result.stdout + result.stderr
    return text, bool(re.search(r"^card \d+:", text, re.MULTILINE))


def stop_recorder(process):
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def record_raw(device, path, seconds, button=None):
    """48kHz·32비트·스테레오로 수신하여 I²S 클록과 채널 배치를 유지한다."""
    if button is not None:
        print("버튼을 놓은 뒤 눌러서 말하세요. 놓으면 녹음을 끝냅니다.", file=sys.stderr)
        button.wait_for_release()
        button.wait_for_press()

    with path.open("wb") as output, tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [
                "arecord",
                "-q",
                "-D",
                device,
                "-t",
                "raw",
                "-f",
                "S32_LE",
                "-r",
                "48000",
                "-c",
                "2",
                "-d",
                str(math.ceil(seconds)),
            ],
            stdout=output,
            stderr=errors,
            env=ENV,
        )
        print("녹음 중…", file=sys.stderr, flush=True)
        started = time.monotonic()
        stopped_by_us = False
        try:
            while process.poll() is None:
                elapsed = time.monotonic() - started
                if elapsed >= seconds or (button is not None and not button.is_pressed):
                    stopped_by_us = True
                    stop_recorder(process)
                    break
                time.sleep(0.02)
        finally:
            stop_recorder(process)
        errors.seek(0)
        message = errors.read().decode("utf-8", errors="replace").strip()
        if process.returncode != 0 and not stopped_by_us:
            raise RuntimeError(f"녹음 실패: {message}")

    if path.stat().st_size < 48000 * 8 * 0.3:
        raise ValueError("녹음이 없거나 너무 짧습니다. 0.3초 이상 말해 주세요.")


def convert_wav(raw_path, wav_path, channel, gain):
    selected = "c0" if channel == "left" else "c1"
    filters = f"pan=mono|c0={selected},highpass=f=80,volume={gain}"
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "s32le",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-i",
            str(raw_path),
            "-af",
            filters,
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ],
        capture_output=True,
        text=True,
        env=ENV,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"WAV 변환 실패: {result.stderr.strip()}")


def audio_info(wav_path):
    with wave.open(str(wav_path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
            raise ValueError("입력 WAV는 16kHz, 모노, 16비트 PCM이어야 합니다.")
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(
            np.float64
        )
    if len(samples) < 4800:
        raise ValueError("0.3초 이상의 녹음이 필요합니다.")
    normalized = samples / 32768.0
    rms = float(np.sqrt(np.mean(normalized**2)))
    return {
        "duration_s": round(len(samples) / 16000, 3),
        "rms_dbfs": round(20 * math.log10(max(rms, 1e-12)), 1),
        "peak": round(float(np.max(np.abs(normalized))), 4),
        "clipped_fraction": round(float(np.mean(np.abs(samples) >= 32767)), 5),
    }


def transcribe(wav_path, binary, model, language, threads, temporary):
    if not binary.is_file():
        raise FileNotFoundError(f"Whisper 실행 파일 없음: {binary}")
    if not model.is_file():
        raise FileNotFoundError(f"Whisper 모델 없음: {model}")
    prefix = temporary / "transcript"
    started = time.monotonic()
    with (temporary / "whisper.log").open("w+") as log:
        result = subprocess.run(
            [
                str(binary),
                "-m",
                str(model),
                "-f",
                str(wav_path),
                "-l",
                language,
                "-t",
                str(threads),
                "-bs",
                "1",
                "-bo",
                "1",
                "-ng",
                "-np",
                "-oj",
                "-of",
                str(prefix),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=ENV,
            timeout=180,
        )
        if result.returncode != 0:
            log.seek(0)
            raise RuntimeError("Whisper 실행 실패: " + log.read()[-3000:])
    data = json.loads(prefix.with_suffix(".json").read_text())
    text = " ".join(segment["text"].strip() for segment in data["transcription"]).strip()
    return text, round(time.monotonic() - started, 3)


def send_tracking_command(command, url):
    request = urllib.request.Request(
        url,
        data=json.dumps(command).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"추적 명령 거절: {detail}") from error
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("추적 연결부가 명령을 수락하지 않았습니다.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--device", help="arecord -l에서 확인한 ALSA 장치명")
    parser.add_argument("--seconds", type=float, default=5, help="녹음 시간 또는 PTT 최대 시간")
    parser.add_argument("--channel", choices=["left", "right"], default="left")
    parser.add_argument("--gain", type=float, default=1, help="고정 디지털 증폭 배율")
    parser.add_argument("--ptt", action="store_true", help="GPIO23 버튼을 누르는 동안 녹음")
    parser.add_argument("--record-only", action="store_true", help="Whisper 없이 녹음만 검사")
    parser.add_argument("--output", type=Path, help="지정할 때만 WAV 파일을 보관")
    parser.add_argument("--input-wav", type=Path, help="마이크 대신 기존 16kHz WAV로 검사")
    parser.add_argument("--parse", action="store_true", help="인식 문장을 기존 Qwen 해석기로 전달")
    parser.add_argument("--send", action="store_true", help="Qwen 해석 후 추적 연결부에 명령 전달")
    parser.add_argument("--controller-url", default="http://127.0.0.1:8082/command")
    parser.add_argument("--language", choices=["ko", "en"], default="ko")
    parser.add_argument("--threads", type=int, choices=range(1, 5), default=2)
    parser.add_argument("--silence-db", type=float, default=-60, help="무음 판정 RMS 기준")
    parser.add_argument(
        "--whisper-bin", type=Path, default=ROOT / "tools/whisper.cpp/build/bin/whisper-cli"
    )
    parser.add_argument("--model", type=Path, default=ROOT / "models/whisper/ggml-base.bin")
    args = parser.parse_args()

    if not 0.3 <= args.seconds <= 15:
        parser.error("--seconds는 0.3~15초 범위입니다.")
    if not math.isfinite(args.gain) or not 0 < args.gain <= 16:
        parser.error("--gain은 0 초과 16 이하여야 합니다.")
    if not math.isfinite(args.silence_db) or not -120 <= args.silence_db <= 0:
        parser.error("--silence-db는 -120~0 범위입니다.")
    if args.record_only and (args.parse or args.send):
        parser.error("--record-only는 --parse/--send와 함께 사용할 수 없습니다.")
    if args.input_wav is not None and args.ptt:
        parser.error("--input-wav와 --ptt는 함께 사용할 수 없습니다.")

    if args.list_devices or args.input_wav is None:
        listing, available = capture_devices()
        if args.list_devices:
            print(listing, end="")
            return
        if not available:
            raise RuntimeError(
                "녹음 장치가 없습니다. I²S 오버레이 설정 후 arecord -l을 확인하세요."
            )
        if not args.device or args.device.lower() in {"default", "null"}:
            parser.error("--device로 실제 hw:/plughw: 녹음 장치를 지정하세요.")

    if not args.record_only:
        if not args.whisper_bin.is_file() or not args.model.is_file():
            raise RuntimeError("Whisper 준비가 필요합니다. logs/whisper-setup.log를 확인하세요.")

    button = factory = None
    try:
        if args.ptt:
            from gpiozero import Button
            from gpiozero.pins.lgpio import LGPIOFactory

            factory = LGPIOFactory()
            button = Button(23, pull_up=True, bounce_time=0.03, pin_factory=factory)

        with tempfile.TemporaryDirectory(prefix="robot-mic-") as directory:
            temporary = Path(directory)
            if args.input_wav is None:
                raw_path = temporary / "input.raw"
                wav_path = temporary / "input.wav"
                record_raw(args.device, raw_path, args.seconds, button)
                convert_wav(raw_path, wav_path, args.channel, args.gain)
            else:
                wav_path = args.input_wav.resolve()

            metrics = audio_info(wav_path)
            result = {"status": "recorded", "audio": metrics}
            if args.output is not None:
                destination = args.output.resolve()
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination != wav_path.resolve():
                    shutil.copyfile(wav_path, destination)
                result["wav"] = str(destination)

            if not args.record_only:
                if metrics["rms_dbfs"] < args.silence_db:
                    result.update(status="no_speech", text="")
                else:
                    text, elapsed = transcribe(
                        wav_path,
                        args.whisper_bin.resolve(),
                        args.model.resolve(),
                        args.language,
                        args.threads,
                        temporary,
                    )
                    result.update(
                        status="transcribed" if text else "no_speech",
                        text=text,
                        transcription_s=elapsed,
                    )
                    if (args.parse or args.send) and text:
                        from .command_parser import normalize_transcript, parse_command

                        command_text = normalize_transcript(text)
                        result["command_text"] = command_text
                        if not command_text:
                            result.update(status="no_speech")
                        else:
                            result["command"] = parse_command(command_text)
                            if args.send:
                                if result["command"]["action"] == "unknown":
                                    result["status"] = "unrecognized_command"
                                    result["delivery"] = {
                                        "ok": False,
                                        "error": "지원하는 추적 명령을 인식하지 못했습니다. 다시 말해 주세요.",
                                    }
                                else:
                                    try:
                                        result["delivery"] = send_tracking_command(
                                            result["command"],
                                            args.controller_url,
                                        )
                                    except (OSError, ValueError, RuntimeError) as error:
                                        result["status"] = "delivery_failed"
                                        result["delivery"] = {"ok": False, "error": str(error)}

            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 1 if result["status"] in {"delivery_failed", "unrecognized_command"} else 0
    finally:
        if button is not None:
            button.close()
        if factory is not None:
            factory.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("녹음/인식 중단", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"마이크 처리 실패: {error}", file=sys.stderr)
        raise SystemExit(1)
