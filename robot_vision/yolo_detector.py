"""YOLO11 NCNN 검출 및 최신 JPEG 프레임 처리. GPIO/모터는 사용하지 않는다."""

import logging
import time
from pathlib import Path
from threading import Condition, Event, Lock, Thread

import cv2
import ncnn
import numpy as np
import yaml


class YoloDetector:
    def __init__(self, model_dir, confidence=0.4, iou=0.45, threads=2):
        folder = Path(model_dir)
        metadata = yaml.safe_load((folder / "metadata.yaml").read_text())
        names = metadata["names"]
        self.names = names if isinstance(names, list) else [names[i] for i in range(len(names))]
        self.height, self.width = map(int, metadata["imgsz"])
        if metadata.get("end2end", False):
            raise ValueError("이 디코더는 end2end=False인 YOLO 검출 모델용입니다.")
        self.confidence = confidence
        self.iou = iou
        self.net = ncnn.Net()
        self.net.opt.use_vulkan_compute = False
        self.net.opt.num_threads = threads
        if self.net.load_param(str(folder / "model.ncnn.param")) != 0:
            raise RuntimeError("NCNN param 로딩 실패")
        if self.net.load_model(str(folder / "model.ncnn.bin")) != 0:
            raise RuntimeError("NCNN 가중치 로딩 실패")

    def detect(self, image):
        height, width = image.shape[:2]
        scale = min(self.width / width, self.height / height)
        resized_w, resized_h = round(width * scale), round(height * scale)
        left = (self.width - resized_w) // 2
        top = (self.height - resized_h) // 2
        resized = cv2.resize(image, (resized_w, resized_h))
        padded = cv2.copyMakeBorder(
            resized,
            top,
            self.height - resized_h - top,
            left,
            self.width - resized_w - left,
            cv2.BORDER_CONSTANT,
            value=(114, 114, 114),
        )
        tensor = ncnn.Mat.from_pixels(
            padded,
            ncnn.Mat.PixelType.PIXEL_BGR2RGB,
            self.width,
            self.height,
        )
        tensor.substract_mean_normalize([], [1 / 255.0] * 3)
        with self.net.create_extractor() as extractor:
            if extractor.input("in0", tensor) != 0:
                raise RuntimeError("NCNN 입력 실패")
            code, output = extractor.extract("out0")
            if code != 0:
                raise RuntimeError("NCNN 추론 실패")
            predictions = np.array(output).copy()
        return self.decode(predictions, width, height, scale, left, top)

    def decode(self, predictions, width, height, scale, left, top):
        # YOLO11 export: [4 + 클래스 수, 후보 수], 좌표는 입력 영상의 cx,cy,w,h.
        channels = 4 + len(self.names)
        if predictions.ndim != 2:
            raise ValueError(f"예상하지 않은 출력 차원: {predictions.shape}")
        if predictions.shape[0] == channels:
            predictions = predictions.T
        elif predictions.shape[1] != channels:
            raise ValueError(f"예상하지 않은 출력 크기: {predictions.shape}")

        classes = predictions[:, 4:].argmax(axis=1)
        scores = predictions[np.arange(len(predictions)), classes + 4]
        mask = np.isfinite(predictions).all(axis=1) & (scores >= self.confidence)
        boxes = predictions[mask, :4].copy()
        scores, classes = scores[mask], classes[mask]
        if not len(boxes):
            return []

        x1 = np.clip((boxes[:, 0] - boxes[:, 2] / 2 - left) / scale, 0, width)
        y1 = np.clip((boxes[:, 1] - boxes[:, 3] / 2 - top) / scale, 0, height)
        x2 = np.clip((boxes[:, 0] + boxes[:, 2] / 2 - left) / scale, 0, width)
        y2 = np.clip((boxes[:, 1] + boxes[:, 3] / 2 - top) / scale, 0, height)
        valid = (x2 > x1) & (y2 > y1)
        xyxy = np.column_stack((x1, y1, x2, y2))[valid]
        scores, classes = scores[valid], classes[valid]
        detections = []
        # 클래스가 다른 겹친 물체는 제거하지 않도록 클래스별 NMS.
        for class_id in np.unique(classes):
            indices = np.flatnonzero(classes == class_id)
            group = xyxy[indices]
            xywh = group.copy()
            xywh[:, 2:] -= xywh[:, :2]
            keep = cv2.dnn.NMSBoxes(
                xywh.tolist(),
                scores[indices].tolist(),
                self.confidence,
                self.iou,
            )
            for offset in np.asarray(keep).reshape(-1):
                index = indices[int(offset)]
                box = xyxy[index]
                detections.append(
                    {
                        "class_id": int(class_id),
                        "label": self.names[int(class_id)],
                        "confidence": round(float(scores[index]), 4),
                        "bbox": [round(float(v), 1) for v in box],
                        "center": [
                            round(float((box[0] + box[2]) / 2), 1),
                            round(float((box[1] + box[3]) / 2), 1),
                        ],
                    }
                )
        return sorted(detections, key=lambda item: item["confidence"], reverse=True)[:100]

    def close(self):
        self.net.clear()


class YoloWorker:
    """카메라를 따로 열지 않고 기존 인코더의 최신 프레임만 처리한다."""

    def __init__(self, source, model_dir, fps=5, confidence=0.4):
        self.source = source
        self.detector = YoloDetector(model_dir, confidence=confidence)
        self.interval = 1.0 / fps
        self.condition = Condition()
        self.lock = Lock()
        self.stop_event = Event()
        self.frame = None
        self.frame_id = -1
        self.state = {"status": "starting", "detections": []}
        self.thread = Thread(target=self.run, name="yolo", daemon=True)

    def start(self):
        self.thread.start()

    def snapshot(self):
        with self.lock:
            state = dict(self.state)
        timestamp = state.get("timestamp")
        age = None if timestamp is None else max(0, time.monotonic() - timestamp)
        state["age_ms"] = None if age is None else round(age * 1000, 1)
        state["stale"] = age is None or age > 1.0
        return state

    def run(self):
        last_frame_id = -1
        cv2.setNumThreads(1)
        while not self.stop_event.is_set():
            with self.source.condition:
                self.source.condition.wait_for(
                    lambda: self.source.frame_id != last_frame_id or self.stop_event.is_set(),
                    timeout=1,
                )
                if self.stop_event.is_set():
                    break
                if self.source.frame_id == last_frame_id or self.source.frame is None:
                    continue
                jpeg = self.source.frame
                frame_id = self.source.frame_id
                timestamp = self.source.timestamp
            last_frame_id = frame_id
            started = time.monotonic()
            try:
                image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    raise ValueError("카메라 JPEG 디코딩 실패")
                detections = self.detector.detect(image)
                inference_ms = (time.monotonic() - started) * 1000
                height, width = image.shape[:2]
                for detection in detections:
                    x1, y1, x2, y2 = map(round, detection["bbox"])
                    cx, cy = map(round, detection["center"])
                    cv2.rectangle(image, (x1, y1), (x2, y2), (0, 230, 0), 2)
                    cv2.circle(image, (cx, cy), 4, (0, 230, 0), -1)
                    text = f"{detection['label']} {detection['confidence']:.2f}"
                    cv2.putText(
                        image,
                        text,
                        (x1, max(16, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.5,
                        (0, 230, 0),
                        1,
                    )
                cv2.drawMarker(
                    image, (width // 2, height // 2), (0, 200, 255), cv2.MARKER_CROSS, 18, 1
                )
                cv2.putText(
                    image,
                    f"YOLO {inference_ms:.0f} ms",
                    (8, height - 10),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 200, 255),
                    1,
                )
                ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if not ok:
                    raise RuntimeError("검출 영상 JPEG 인코딩 실패")
                with self.lock:
                    self.state = {
                        "status": "ok",
                        "frame_id": frame_id,
                        # 인코더가 JPEG를 전달한 시각이며 센서 노출 시각은 아니다.
                        "timestamp": timestamp,
                        "timestamp_source": "jpeg_ready_monotonic",
                        "frame_width": width,
                        "frame_height": height,
                        "inference_ms": round(inference_ms, 1),
                        "detections": detections,
                    }
                with self.condition:
                    self.frame = encoded.tobytes()
                    self.frame_id = frame_id
                    self.condition.notify_all()
            except Exception as error:
                logging.exception("YOLO 프레임 처리 실패")
                with self.lock:
                    self.state = {
                        "status": "error",
                        "error": str(error),
                        "frame_id": frame_id,
                        "timestamp": timestamp,
                        "detections": [],
                    }
            self.stop_event.wait(max(0, self.interval - (time.monotonic() - started)))

    def close(self):
        self.stop_event.set()
        with self.source.condition:
            self.source.condition.notify_all()
        self.thread.join(timeout=10)
        if not self.thread.is_alive():
            self.detector.close()
