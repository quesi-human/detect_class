#!/usr/bin/python3
"""프로젝트 위치와 실행 계정에 맞춰 systemd 서비스와 관리 명령을 설치한다."""

import argparse
import os
import pwd
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
PROJECT = SOURCE.parent


def unit_value(value):
    """systemd 명령행에서 실행 파일 경로의 공백과 특수 문자를 보존한다."""
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def install_text(text, destination, mode):
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chown(path, 0, 0)
    path.chmod(mode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=os.environ.get("SUDO_USER"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("sudo python3 deployment/install_service.py 로 실행하세요.")
    username = args.user or pwd.getpwuid(PROJECT.stat().st_uid).pw_name
    account = pwd.getpwnam(username)
    if account.pw_uid == 0:
        parser.error("--user로 서비스를 실행할 일반 계정을 지정하세요.")
    python = PROJECT / ".venv/bin/python"
    if not python.is_file():
        parser.error("프로젝트의 .venv를 먼저 생성하세요.")
    if subprocess.run(["systemctl", "is-active", "--quiet", "pjt-robot.service"]).returncode == 0:
        parser.error("실행 중인 서비스를 먼저 정지하세요: sudo systemctl stop pjt-robot.service")

    hardware = (SOURCE / "pjt-robot-hardware").read_text().replace("@USER@", username)
    control = (SOURCE / "pjt-robot-control").read_text()
    for name, text in [("hardware", hardware), ("control", control)]:
        compile(text, name, "exec")
    subprocess.run(["/bin/sh", "-n", str(SOURCE / "pjt-robot-ssh")], check=True)
    unit = (SOURCE / "pjt-robot.service").read_text()
    for placeholder, value in {
        "@USER@": username,
        "@ROOT@": str(PROJECT).replace("%", "%%"),
        "@PYTHON@": unit_value(python),
    }.items():
        unit = unit.replace(placeholder, value)
    policy = (
        username
        + " ALL=(root) NOPASSWD: "
        + ", ".join(
            "/usr/local/sbin/pjt-robot-control " + action
            for action in ("start", "stop", "restart", "status", "logs")
        )
        + "\n"
    )
    destinations = {
        "/usr/local/libexec/pjt-robot-hardware": (hardware, 0o755),
        "/usr/local/sbin/pjt-robot-control": (control, 0o755),
        "/usr/local/bin/pjt-robot-ssh": ((SOURCE / "pjt-robot-ssh").read_text(), 0o755),
        "/etc/systemd/system/pjt-robot.service": (unit, 0o644),
        "/etc/sudoers.d/pjt-robot-vision": (policy, 0o440),
    }
    backup = Path("/var/backups/pjt-robot") / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True)
    for destination in destinations:
        path = Path(destination)
        if path.exists():
            shutil.copy2(path, backup / path.name)
    with tempfile.TemporaryDirectory(prefix="pjt-install-") as directory:
        temporary = Path(directory) / "sudoers"
        temporary.write_text(policy)
        subprocess.run(["/usr/sbin/visudo", "-cf", str(temporary)], check=True)
        for destination, (text, mode) in destinations.items():
            install_text(text, destination, mode)
    subprocess.run(
        ["/usr/bin/systemd-analyze", "verify", "/etc/systemd/system/pjt-robot.service"], check=True
    )

    environment = Path("/etc/pjt-robot-vision.env")
    if not environment.exists():
        install_text((PROJECT / "config/robot-vision.env.example").read_text(), environment, 0o644)
    subprocess.run(["/usr/bin/systemctl", "daemon-reload"], check=True)
    print(f"서비스 설치 완료: user={username}, project={PROJECT}")
    print(f"이전 설치 파일 백업: {backup}")
    print("실행: sudo systemctl start pjt-robot.service")


if __name__ == "__main__":
    main()
