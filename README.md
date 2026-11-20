# Robot Vision

라즈베리파이 5에서 음성 명령을 받아 대상을 검출하고, 2축 서보로 카메라 방향을 조정하는 프로젝트다. 음성 인식과 명령 해석, 영상 검출을 모두 장치 안에서 처리한다.

- **Whisper base**: 마이크 녹음을 한국어 문장으로 변환
- **Qwen3 1.7B / llama.cpp**: 문장을 검증 가능한 JSON 명령으로 변환
- **YOLO11n / NCNN**: 카메라 영상에서 COCO 80개 클래스 검출
- **MG996R + SG90**: 대상의 중심을 화면 중앙으로 이동
- **버튼·LED·OLED**: 녹음 시작, 이동 방향, 추적 상태 표시

## 현재 동작

버튼을 한 번 누르면 5초간 녹음한다. “사람을 따라가”, “의자를 봐”, “휴대폰을 찾아”처럼 대상을 말하면 해당 클래스에서 **바운딩 박스 면적이 가장 큰 객체**를 매 프레임 선택한다. 객체 중심과 화면 중심의 오차를 이용해 좌우·상하를 보정한다.

대상이 보이지 않으면 현재 자세에서 대기하고, 다시 나타나면 추적을 재개한다. ID 추적과 자동 왕복 탐색은 사용하지 않는다. 박스 크기는 근접도에 대한 근사 기준이며 실제 거리 측정값은 아니다.

OLED 하단은 `이동 상태 | 대상` 형식이다. `LEFT/RIGHT`는 MG996R, `UP/DOWN`은 SG90의 방향이며, 두 축 중 가장 최근 이동 명령을 표시한다. 대기 시 `WAIT`, 이동 명령이 없으면 `STOP`을 표시한다.

## 구성

```text
robot_vision/   카메라·음성·검출·모터 제어 패키지
config/         장치 설정 예시
deployment/     systemd 서비스와 설치 도구
scripts/        모델 다운로드·엔진 빌드·원격 관리
examples/       단독 하드웨어 시험
tests/          하드웨어 없이 실행하는 검사
docs/           설치, 배선, 구조, 사용법
```

`models/`, `tools/`, `.venv/`, 녹음·로그는 장치에서 별도로 준비하며 Git에 포함하지 않는다.

## 시작하기

Raspberry Pi OS 64비트 환경에서 [설치 안내](docs/setup.md)에 따라 의존성과 모델을 준비한다. 실제 검증 환경은 Raspberry Pi 5 8GB, Raspberry Pi OS Trixie, Python 3.13이다.

```bash
# 설정·필수 파일 검사
.venv/bin/python -m robot_vision --check

# 서비스 설치 및 실행
sudo .venv/bin/python deployment/install_service.py
sudo systemctl start pjt-robot.service
```

영상은 `http://<라파IP>:8080/yolo.mjpg`에서 확인한다. 노트북에서 관리하려면 [사용법](docs/usage.md)의 SSH 키 설정과 `scripts/robotctl`을 사용한다.

## 문서

- [설치와 모델 준비](docs/setup.md)
- [하드웨어 배선과 핀 번호](docs/hardware.md)
- [실행, 명령, 문제 확인](docs/usage.md)
- [모듈 구조와 데이터 흐름](docs/architecture.md)

## 검사

장치와 모델 없이도 Python 3.11 이상의 별도 개발 환경에서 단위 검사를 실행할 수 있다.

```bash
python3 -m venv .venv-dev
.venv-dev/bin/python -m pip install -r requirements-dev.txt
.venv-dev/bin/python -m unittest discover -s tests -v
.venv-dev/bin/python -m ruff check .
.venv-dev/bin/python -m ruff format --check .
```

GitHub Actions와 GitLab CI에도 같은 검사를 등록했다. 실제 카메라·마이크·모터 동작은 라파에서 따로 확인해야 한다.
