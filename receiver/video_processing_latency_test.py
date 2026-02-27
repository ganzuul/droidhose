#!/usr/bin/env python3
import argparse
import ctypes
import fcntl
import os
import statistics
import threading
import time

import cv2
import numpy as np


V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_FIELD_NONE = 1
V4L2_COLORSPACE_JPEG = 7
V4L2_PIX_FMT_YUV420 = 0x32315559


class _v4l2_pix_format(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pixelformat", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("bytesperline", ctypes.c_uint32),
        ("sizeimage", ctypes.c_uint32),
        ("colorspace", ctypes.c_uint32),
        ("priv", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("ycbcr_enc", ctypes.c_uint32),
        ("quantization", ctypes.c_uint32),
        ("xfer_func", ctypes.c_uint32),
    ]


class _v4l2_fmt_union(ctypes.Union):
    _fields_ = [
        ("pix", _v4l2_pix_format),
        ("_pad", ctypes.c_uint8 * 200),
        ("_align", ctypes.c_uint64),
    ]


class _v4l2_format(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("fmt", _v4l2_fmt_union),
    ]


def _iowr(ioc_type: int, nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ioc_type << 8) | nr


VIDIOC_S_FMT = _iowr(ord("V"), 5, ctypes.sizeof(_v4l2_format))


def setup_v4l2_output(device: str, width: int, height: int) -> int:
    fd = os.open(device, os.O_WRONLY)
    fmt = _v4l2_format()
    fmt.type = V4L2_BUF_TYPE_VIDEO_OUTPUT
    fmt.fmt.pix.width = width
    fmt.fmt.pix.height = height
    fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_YUV420
    fmt.fmt.pix.field = V4L2_FIELD_NONE
    fmt.fmt.pix.bytesperline = width
    fmt.fmt.pix.sizeimage = width * height * 3 // 2
    fmt.fmt.pix.colorspace = V4L2_COLORSPACE_JPEG
    fcntl.ioctl(fd, VIDIOC_S_FMT, fmt)
    return fd


def encode_timestamp_bits(img: np.ndarray, t_us: int, bits: int, bar_h: int) -> None:
    h, w = img.shape[:2]
    block_w = max(2, w // bits)
    img[:bar_h, :] = 0
    for i in range(bits):
        bit = (t_us >> i) & 1
        x0 = i * block_w
        x1 = w if i == bits - 1 else min(w, (i + 1) * block_w)
        img[0:bar_h, x0:x1] = 255 if bit else 0

    cv2.putText(
        img,
        f"gen_us={t_us}",
        (10, h - 16),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (220, 220, 220),
        1,
        cv2.LINE_AA,
    )


def decode_timestamp_bits(frame: np.ndarray, bits: int, bar_h: int) -> int:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape[:2]
    block_w = max(2, w // bits)
    sample_y = min(bar_h - 1, max(0, bar_h // 2))

    t_us = 0
    for i in range(bits):
        x0 = i * block_w
        x1 = w if i == bits - 1 else min(w, (i + 1) * block_w)
        if x1 <= x0:
            continue
        val = gray[sample_y, x0:x1].mean()
        if val > 127:
            t_us |= (1 << i)
    return t_us


def percentile(values, p):
    if not values:
        return 0.0
    idx = (len(values) - 1) * p / 100.0
    lo = int(np.floor(idx))
    hi = int(np.ceil(idx))
    if lo == hi:
        return float(values[lo])
    frac = idx - lo
    return float(values[lo] * (1.0 - frac) + values[hi] * frac)


def main() -> None:
    ap = argparse.ArgumentParser(description="Client-only video processing latency test via v4l2loopback")
    ap.add_argument("--device", default="/dev/video2")
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--warmup", type=float, default=1.0)
    ap.add_argument("--bits", type=int, default=40, help="bit-width of embedded timestamp")
    ap.add_argument("--bar-height", type=int, default=24)
    ap.add_argument("--show", action="store_true", help="show captured frames while measuring")
    args = ap.parse_args()

    if args.bits <= 0 or args.bits > 52:
        raise SystemExit("--bits must be between 1 and 52")

    frame_period = 1.0 / args.fps
    stop_evt = threading.Event()
    writer_ready = threading.Event()

    write_fd = None
    lat_ms = []

    def writer_thread() -> None:
        nonlocal write_fd
        write_fd = setup_v4l2_output(args.device, args.width, args.height)
        writer_ready.set()
        next_t = time.monotonic()
        try:
            while not stop_evt.is_set():
                now_ns = time.monotonic_ns()
                now_us = now_ns // 1000

                bgr = np.zeros((args.height, args.width, 3), dtype=np.uint8)
                encode_timestamp_bits(bgr, now_us, args.bits, args.bar_height)

                i420 = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)
                os.write(write_fd, i420.tobytes())

                next_t += frame_period
                sleep_s = next_t - time.monotonic()
                if sleep_s > 0:
                    time.sleep(sleep_s)
                else:
                    next_t = time.monotonic()
        finally:
            if write_fd is not None:
                os.close(write_fd)

    wt = threading.Thread(target=writer_thread, daemon=True)
    wt.start()

    if not writer_ready.wait(timeout=2.0):
        raise SystemExit("Failed to initialize v4l2 writer")

    cap = cv2.VideoCapture(args.device, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(args.width))
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(args.height))
    cap.set(cv2.CAP_PROP_FPS, float(args.fps))

    if not cap.isOpened():
        stop_evt.set()
        raise SystemExit(f"Cannot open capture device {args.device}")

    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < args.duration + args.warmup:
            ok, frame = cap.read()
            if not ok:
                continue

            recv_us = time.monotonic_ns() // 1000
            sent_us = decode_timestamp_bits(frame, args.bits, args.bar_height)
            sample_ms = (recv_us - sent_us) / 1000.0

            if time.monotonic() - t0 >= args.warmup and sample_ms >= 0:
                lat_ms.append(sample_ms)

            if args.show:
                cv2.putText(
                    frame,
                    f"client latency ~ {sample_ms:.2f} ms",
                    (10, 40),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.9,
                    (0, 255, 0),
                    2,
                    cv2.LINE_AA,
                )
                cv2.imshow("video_processing_latency_test", frame)
                if cv2.waitKey(1) & 0xFF == 27:
                    break
    finally:
        stop_evt.set()
        wt.join(timeout=1.5)
        cap.release()
        if args.show:
            cv2.destroyAllWindows()

    if not lat_ms:
        raise SystemExit("No latency samples collected")

    lat_ms.sort()
    print(f"Samples: {len(lat_ms)}")
    print(f"mean: {statistics.fmean(lat_ms):.2f} ms")
    print(f"min : {lat_ms[0]:.2f} ms")
    print(f"p50 : {percentile(lat_ms, 50):.2f} ms")
    print(f"p90 : {percentile(lat_ms, 90):.2f} ms")
    print(f"p95 : {percentile(lat_ms, 95):.2f} ms")
    print(f"p99 : {percentile(lat_ms, 99):.2f} ms")
    print(f"max : {lat_ms[-1]:.2f} ms")


if __name__ == "__main__":
    main()
