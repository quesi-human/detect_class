"""설정·모델·실행 도구는 프로젝트 루트를 기준으로 찾는다."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
