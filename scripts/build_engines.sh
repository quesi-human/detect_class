#!/usr/bin/env bash
# 검증한 커밋으로 llama.cpp와 whisper.cpp를 빌드한다.
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
jobs=${BUILD_JOBS:-2}
mkdir -p "$project_root/tools"
build_engine() {
  local name=$1 repository=$2 revision=$3
  shift 3
  local destination="$project_root/tools/$name"
  if [[ ! -d "$destination/.git" ]]; then
    mkdir -p "$destination"
    git -C "$destination" init
    git -C "$destination" remote add origin "$repository"
  fi
  if [[ -n "$(git -C "$destination" status --porcelain)" ]]; then
    echo "로컬 수정이 있어 중단합니다: $destination" >&2
    exit 1
  fi
  if ! git -C "$destination" cat-file -e "$revision^{commit}" 2>/dev/null; then
    git -C "$destination" fetch --depth 1 origin "$revision"
  fi
  git -C "$destination" checkout --detach "$revision"
  cmake -S "$destination" -B "$destination/build" -DCMAKE_BUILD_TYPE=Release "$@"
}
build_engine llama.cpp https://github.com/ggml-org/llama.cpp.git \
  e4b620af19f02960dea7a3a81de7c5805aa3a333 -DGGML_NATIVE=ON -DLLAMA_BUILD_TESTS=OFF
cmake --build "$project_root/tools/llama.cpp/build" --target llama-server llama-cli -j "$jobs"
build_engine whisper.cpp https://github.com/ggml-org/whisper.cpp.git \
  6680804f99c3982b7d59f27ae315ce880570017e -DGGML_NATIVE=ON -DWHISPER_BUILD_TESTS=OFF
cmake --build "$project_root/tools/whisper.cpp/build" --target whisper-cli -j "$jobs"
