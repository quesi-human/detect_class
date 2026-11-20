"""모든 소비자에게 전달하기 전에 카메라 JPEG 방향을 통일한다."""

import cv2
import numpy as np


def rotate_jpeg(jpeg, clockwise_degrees=0):
    rotations = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }
    if clockwise_degrees == 0:
        return bytes(jpeg)
    if clockwise_degrees not in rotations:
        raise ValueError("영상 회전은 0, 90, 180, 270도만 지원합니다.")
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("카메라 JPEG 디코딩 실패")
    image = cv2.rotate(image, rotations[clockwise_degrees])
    ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise RuntimeError("회전 영상 JPEG 인코딩 실패")
    return encoded.tobytes()
