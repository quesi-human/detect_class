# 사용법

## 기본 실행

```bash
sudo systemctl start pjt-robot.service
sudo systemctl stop pjt-robot.service
sudo systemctl restart pjt-robot.service
journalctl -u pjt-robot.service -n 80 --no-pager
```

서비스는 카메라·Qwen·YOLO·OLED 준비를 확인한 뒤 버튼 입력을 받는다. 버튼은 한 번 누르고 떼면 된다. 5초 녹음 후 Whisper와 Qwen이 차례로 처리하므로 명령 전달까지 시간이 걸린다.

예: “사람을 따라가”, “의자를 봐”, “책을 찾아”, “휴대폰을 봐”, “추적 멈춰”. 지원 클래스의 한국어 명칭은 `robot_vision/object_catalog.py`에 있다. 색상 조건과 COCO에 없는 블록 등은 현재 지원하지 않는다.

## 노트북에서 관리

일반 SSH 로그인과 원격 서비스 관리 키를 구분한다. 설치된 `pjt-robot-ssh`는 `start`, `stop`, `restart`, `status`, `logs`만 허용한다. 전용 공개키를 라파의 `~/.ssh/authorized_keys`에 등록할 때 다음 제한을 붙인다.

```text
restrict,command="/usr/local/bin/pjt-robot-ssh" ssh-ed25519 <전용 공개키>
```

개인키·비밀번호·실제 `authorized_keys` 파일은 저장소에 넣지 않는다. 먼저 일반 SSH로 호스트 키를 확인한 뒤 전용 키를 사용한다.

```bash
export ROBOT_HOST=<라파IP>
export ROBOT_USER=<라파계정>
export ROBOT_SSH_KEY="$HOME/.ssh/id_ed25519_pjt_robot_vision"
bash scripts/robotctl start
bash scripts/robotctl status
bash scripts/robotctl stop
```

전용 키는 터미널 로그인을 허용하지 않는다. 일반 로그인에는 별도의 일반 키나 비밀번호 인증을 사용한다.

## 영상과 상태

| 주소 | 내용 |
|---|---|
| `http://<라파IP>:8080/stream.mjpg` | 회전 적용된 카메라 영상 |
| `http://<라파IP>:8080/yolo.mjpg` | 박스·중심점·클래스 표시 영상 |
| `http://<라파IP>:8080/detections.json` | 최신 검출 JSON |
| `http://<라파IP>:8080/oled-status.json` | OLED 초기화·영상·표시 상태 |
| `http://127.0.0.1:8082/status` | 라파 내부 제어 상태 |

기존 대시보드 호환을 위해 `/gpio-blink?pin=...`, `/oled-display?text=...`도 유지한다. 추적 중에는 방향 LED를 제어부가 소유하므로 같은 GPIO에 별도 점멸 요청을 보내지 않는다.

라파에서 직접 목표를 지정하려면:

```bash
curl -s -X POST http://127.0.0.1:8082/command \
  -H 'Content-Type: application/json' \
  -d '{"action":"look_at","color":null,"object":"chair"}'
```

정지 명령은 `{"action":"stop","color":null,"object":null}`이다. 대상이 사라져 `waiting_for_target`이면 현재 자세를 유지하며, 새 대상이 보이면 같은 클래스의 최대 박스를 선택한다.

## 설정과 단독 확인

`/etc/pjt-robot-vision.env`를 수정한 후 서비스를 재시작한다. 항목은 `config/robot-vision.env.example`을 참고한다. 방향이 반대면 해당 축의 `MOTOR_REVERSE` 또는 `TILT_REVERSE`를 바꾼다.

```bash
# 버튼만 확인
.venv/bin/python -m robot_vision.button_voice --button-test
# 마이크 장치 목록
.venv/bin/python -m robot_vision.microphone --list-devices
# 녹음과 인식만 확인
.venv/bin/python -m robot_vision.microphone --device hw:CARD=SPH0645,DEV=0 --seconds 5
# LED만 확인
.venv/bin/python -m robot_vision.led --test
```

단독 장치 시험 전에는 전체 서비스를 정지한다. `examples/servo_sequence.py`는 물리33번 MG996R 전용 시험이다. 모델 없이 API 연결부를 확인하려면 `python -m robot_vision.tracking_controller`로 모의 제어부를 실행할 수 있다.

## 문제 확인

- 영상이 멈추면 `detections.json`의 `age_ms`, `stale`과 카메라 로그를 확인한다. 센서 타임아웃은 전원을 끈 뒤 리본 케이블 체결·장력을 확인한다.
- 음성이 잘못 인식되면 로그의 `text`, `command_text`, `command`를 확인한다. 최신 녹음은 `recordings/latest-command.wav` 하나로 덮어쓴다.
- 대상이 보여도 움직이지 않으면 `selected`, `last_result.error_x/error_y`, `axes`를 확인한다. 한계각 또는 중앙 허용 범위에서는 추가 이동하지 않는다.
- `STOP`은 출력 신호 해제와 다르다. 추적 정지는 현재 목표 자세를 유지하고, 서비스 종료 시 PWM을 해제한다.

## GitHub·GitLab에 업로드

모델·빌드 결과·녹음·개인 설정은 `.gitignore`로 제외된다. 소스 파일을 확인한 뒤 커밋하고, 준비한 두 저장소를 별도 remote로 등록한다. 아래 URL은 실제 저장소 주소로 바꾼다.

```bash
git add .
git commit -m "Prepare Raspberry Pi camera tracking project"
git remote add github <GitHub-저장소-URL>
git remote add gitlab <GitLab-저장소-URL>
git push -u github main
git push -u gitlab main
```
