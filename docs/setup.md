# 설치

Raspberry Pi OS 64비트와 라파 카메라, [배선](hardware.md)이 준비된 상태를 기준으로 한다. 아래 명령은 프로젝트 루트에서 실행한다.

## 1. OS 패키지와 가상환경

```bash
sudo apt update
sudo apt install -y python3-venv python3-picamera2 python3-opencv python3-numpy \
  python3-pil python3-yaml python3-gpiozero python3-lgpio python3-rpi-lgpio \
  python3-spidev ffmpeg alsa-utils git curl cmake build-essential libssl-dev libcurl4-openssl-dev
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
```

Picamera2와 GPIO 라이브러리는 Raspberry Pi OS 패키지를 사용한다. 저장소에는 현재 장치에서 사용한 NCNN·luma.oled 버전을 기록했다.

## 2. 부팅 설정

`/boot/firmware/config.txt`를 백업한 뒤 `[all]` 영역에서 다음 설정을 확인한다. 기존에 같은 설정이 있다면 중복해서 추가하지 않는다.

```ini
[all]
dtparam=spi=on
dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4
dtoverlay=adau7002-simple,card-name=SPH0645
```

변경했다면 재부팅한다. GPIO18·19는 마이크 I²S에 사용하므로 서보 PWM은 GPIO12·13을 사용한다.

```bash
pinctrl get 12-13
ls /sys/class/pwm/
arecord -l
```

## 3. 실행 엔진과 모델

```bash
BUILD_JOBS=2 bash scripts/build_engines.sh
bash scripts/download_models.sh
```

엔진 빌드 스크립트는 검증한 llama.cpp·whisper.cpp 커밋을 사용한다. 기본 병렬 빌드 수는 2다. 모델 다운로드에는 Qwen 약 1.1GB, Whisper 약 148MB가 필요하다.

YOLO NCNN 모델은 개발 PC에서 내보낸 뒤 라파로 복사한다. 개발 PC의 별도 가상환경에 Ultralytics를 설치한 후 실행한다.

```bash
yolo export model=yolo11n.pt format=ncnn imgsz=320
```

내보낸 `yolo11n_ncnn_model` 폴더를 라파의 `models/yolo/` 아래에 둔다. 필요한 파일 구조는 다음과 같다.

```text
models/
├── qwen/Qwen3-1.7B-Q4_K_M.gguf
├── whisper/ggml-base.bin
└── yolo/yolo11n_ncnn_model/
    ├── metadata.yaml
    ├── model.ncnn.param
    └── model.ncnn.bin
```

`tools/llama.cpp/build/bin/llama-server`와 `tools/whisper.cpp/build/bin/whisper-cli`도 필요하다. 모델·외부 엔진은 각각의 배포 조건을 따른다.

## 4. 서비스 설치

```bash
.venv/bin/python -m robot_vision --check
sudo .venv/bin/python deployment/install_service.py
sudo systemctl start pjt-robot.service
sudo systemctl status pjt-robot.service
```

설치 도구는 실행 계정과 현재 프로젝트 경로에 맞춰 유닛을 생성한다. 기존 `/etc/pjt-robot-vision.env`는 유지하며, 없으면 `config/robot-vision.env.example`을 복사한다. 다른 계정을 지정하려면 `--user 계정명`을 사용한다. 재설치 전에는 서비스를 정지한다.

자동 부팅이 필요하면 별도로 `sudo systemctl enable pjt-robot.service`를 실행한다.
