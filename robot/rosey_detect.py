#!/usr/bin/env python3
"""Object detection for Rosey: NanoDet-Plus (OpenCV model zoo, 80 COCO classes).

    from rosey_detect import Detector
    det = Detector()
    for d in det(frame_bgr): print(d.name, d.score, d.box)   # box = x1, y1, x2, y2 in pixels

CLI test:  python3 rosey_detect.py image.jpg  [out.jpg]
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, "models", "object_detection_nanodet_2022nov.onnx")
SIZE = 416
STRIDES = (8, 16, 32)
REG_MAX = 7
MEAN = np.array([103.53, 116.28, 123.675], dtype=np.float32)   # BGR
STD = np.array([57.375, 57.12, 58.395], dtype=np.float32)

COCO = ("person bicycle car motorcycle airplane bus train truck boat traffic_light fire_hydrant stop_sign "
        "parking_meter bench bird cat dog horse sheep cow elephant bear zebra giraffe backpack umbrella "
        "handbag tie suitcase frisbee skis snowboard sports_ball kite baseball_bat baseball_glove skateboard "
        "surfboard tennis_racket bottle wine_glass cup fork knife spoon bowl banana apple sandwich orange "
        "broccoli carrot hot_dog pizza donut cake chair couch potted_plant bed dining_table toilet tv laptop "
        "mouse remote keyboard cell_phone microwave oven toaster sink refrigerator book clock vase scissors "
        "teddy_bear hair_drier toothbrush").split()


@dataclass
class Detection:
    cls: int
    score: float
    box: tuple          # x1, y1, x2, y2 in original-image pixels

    @property
    def name(self) -> str:
        return COCO[self.cls].replace("_", " ")


def _grid(stride: int) -> np.ndarray:
    n = int(np.ceil(SIZE / stride))
    xs, ys = np.meshgrid(np.arange(n) * stride, np.arange(n) * stride)
    return np.stack([xs.ravel(), ys.ravel()], axis=-1).astype(np.float32) + 0.5 * (stride - 1)


class Detector:
    def __init__(self, threads: int = 2):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads        # leave CPU for speech-to-text
        so.log_severity_level = 3
        # Don't busy-wait between runs: with the detector idle most of the time (fall watch
        # ~2/s) spinning threads burned more than a whole CPU core for nothing.
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        so.add_session_config_entry("session.inter_op.allow_spinning", "0")
        self.sess = ort.InferenceSession(MODEL, so, providers=["CPUExecutionProvider"])
        self.input = self.sess.get_inputs()[0].name
        outs = self.sess.get_outputs()
        # Match outputs to (stride, kind) by shape rather than by name.
        self.order = []
        for stride in STRIDES:
            n = int(np.ceil(SIZE / stride)) ** 2
            cls_i = next(i for i, o in enumerate(outs) if o.shape[1] == n and o.shape[2] == 80)
            reg_i = next(i for i, o in enumerate(outs) if o.shape[1] == n and o.shape[2] == 4 * (REG_MAX + 1))
            self.order.append((stride, cls_i, reg_i, _grid(stride)))
        self.project = np.arange(REG_MAX + 1, dtype=np.float32)

    def __call__(self, img: np.ndarray, threshold: float = 0.35, classes=None) -> "list[Detection]":
        h, w = img.shape[:2]
        scale = SIZE / max(h, w)                  # letterbox: keep aspect ratio, pad bottom/right
        nh, nw = int(round(h * scale)), int(round(w * scale))
        canvas = np.zeros((SIZE, SIZE, 3), dtype=np.float32)
        canvas[:nh, :nw] = cv2.resize(img, (nw, nh)).astype(np.float32)
        blob = ((canvas - MEAN) / STD).transpose(2, 0, 1)[None]
        outs = self.sess.run(None, {self.input: blob})

        boxes, scores, labels = [], [], []
        for stride, cls_i, reg_i, centers in self.order:
            cls = outs[cls_i][0]
            best = cls.argmax(1)
            conf = cls[np.arange(len(best)), best]
            keep = conf > threshold
            if classes is not None:
                keep &= np.isin(best, classes)
            if not keep.any():
                continue
            reg = outs[reg_i][0][keep].reshape(-1, 4, REG_MAX + 1)
            reg = np.exp(reg - reg.max(-1, keepdims=True))
            reg = (reg / reg.sum(-1, keepdims=True)) @ self.project * stride    # distances l, t, r, b
            c = centers[keep]
            b = np.stack([c[:, 0] - reg[:, 0], c[:, 1] - reg[:, 1], c[:, 0] + reg[:, 2], c[:, 1] + reg[:, 3]], 1)
            boxes.append(b / scale)
            scores.append(conf[keep])
            labels.append(best[keep])
        if not boxes:
            return []
        boxes, scores, labels = np.concatenate(boxes), np.concatenate(scores), np.concatenate(labels)
        xywh = [[float(x1), float(y1), float(x2 - x1), float(y2 - y1)] for x1, y1, x2, y2 in boxes]
        idx = cv2.dnn.NMSBoxesBatched(xywh, scores.tolist(), labels.tolist(), threshold, 0.5)
        out = []
        for i in np.array(idx).ravel():
            x1, y1, x2, y2 = boxes[i]
            out.append(Detection(int(labels[i]), float(scores[i]),
                                 (max(0.0, x1), max(0.0, y1), min(w - 1.0, x2), min(h - 1.0, y2))))
        return sorted(out, key=lambda d: -d.score)


if __name__ == "__main__":
    import time
    img = cv2.imread(sys.argv[1])
    det = Detector()
    det(img)
    t = time.time()
    found = det(img)
    print("%.0f ms" % ((time.time() - t) * 1000))
    for d in found:
        print("%-12s %.2f  %s" % (d.name, d.score, tuple(int(v) for v in d.box)))
        x1, y1, x2, y2 = map(int, d.box)
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 200, 0), 2)
        cv2.putText(img, "%s %.2f" % (d.name, d.score), (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 0), 1)
    if len(sys.argv) > 2:
        cv2.imwrite(sys.argv[2], img)
