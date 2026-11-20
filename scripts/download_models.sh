#!/usr/bin/env bash
# 중단된 다운로드는 기존 파일 끝에서 이어받는다. NCNN 모델은 별도 반입한다.
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$project_root/models/qwen" "$project_root/models/whisper"
download() {
  local destination=$1 url=$2
  if [[ -s "$destination" ]]; then
    echo "기존 모델 사용: $destination"
    return
  fi
  curl -fL --retry 3 -C - -o "$destination.part" "$url"
  mv -- "$destination.part" "$destination"
}
download "$project_root/models/qwen/Qwen3-1.7B-Q4_K_M.gguf" \
  https://huggingface.co/unsloth/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf
download "$project_root/models/whisper/ggml-base.bin" \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin
