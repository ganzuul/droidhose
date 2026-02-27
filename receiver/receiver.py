#!/usr/bin/env python3
"""
droidhose receiver
==================
Connects to the droidhose Android app (via an ADB-forwarded TCP socket),
reads the raw I420 YUV stream, and writes every frame to a v4l2loopback
device so that any V4L2-aware application (OpenTrack, OBS, ffplay …) can
consume it as a regular webcam.

Prerequisites
─────────────
1. Load v4l2loopback (once per boot):
       sudo modprobe v4l2loopback devices=1 video_nr=2 \\
           card_label=droidhose exclusive_caps=1

2. Forward the phone port over USB:
       adb forward tcp:8080 tcp:8080

3. Launch the droidhose app on the phone, then run this script:
       python3 receiver.py

Usage
─────
    receiver.py [--host HOST] [--port PORT] [--device DEV]

    --host    HOST   TCP host to connect to   (default: localhost)
    --port    PORT   TCP port                 (default: 8080)
    --device  DEV    v4l2loopback device path (default: /dev/video2)
"""

import argparse
import ctypes
import fcntl
import os
import socket
import struct
import sys

# ── v4l2 constants & structures ───────────────────────────────────────────────
# From <linux/videodev2.h>

V4L2_BUF_TYPE_VIDEO_OUTPUT = 2
V4L2_FIELD_NONE            = 1
V4L2_COLORSPACE_JPEG       = 7   # full-range YCbCr, widely supported
V4L2_PIX_FMT_YUV420        = 0x32315559   # fourcc 'YU12' (I420 planar)


class _v4l2_pix_format(ctypes.Structure):
    _fields_ = [
        ("width",        ctypes.c_uint32),
        ("height",       ctypes.c_uint32),
        ("pixelformat",  ctypes.c_uint32),
        ("field",        ctypes.c_uint32),
        ("bytesperline", ctypes.c_uint32),
        ("sizeimage",    ctypes.c_uint32),
        ("colorspace",   ctypes.c_uint32),
        ("priv",         ctypes.c_uint32),
        ("flags",        ctypes.c_uint32),
        ("ycbcr_enc",    ctypes.c_uint32),
        ("quantization", ctypes.c_uint32),
        ("xfer_func",    ctypes.c_uint32),
    ]


class _v4l2_fmt_union(ctypes.Union):
    _fields_ = [
        ("pix",    _v4l2_pix_format),
        ("_pad",   ctypes.c_uint8 * 200),
        # v4l2_window inside the kernel union contains pointer members, forcing
        # 8-byte alignment on 64-bit Linux.  The _align field reproduces that
        # alignment so ctypes computes the correct sizeof (208) and therefore
        # the correct VIDIOC_S_FMT ioctl code (0xC0D05605 on x86_64).
        ("_align", ctypes.c_uint64),
    ]


class _v4l2_format(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("fmt",  _v4l2_fmt_union),
    ]


def _iowr(ioc_type: int, nr: int, size: int) -> int:
    """Compute a Linux _IOWR ioctl request code."""
    return (3 << 30) | (size << 16) | (ioc_type << 8) | nr


VIDIOC_S_FMT = _iowr(ord("V"), 5, ctypes.sizeof(_v4l2_format))

# ── wire protocol ─────────────────────────────────────────────────────────────
HEADER_MAGIC = b"DHDR"
HEADER_SIZE  = 12   # magic(4) + width(4) + height(4), little-endian


# ── helpers ───────────────────────────────────────────────────────────────────

def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly *n* bytes from *sock*, raising EOFError on close."""
    buf  = bytearray(n)
    view = memoryview(buf)
    got  = 0
    while got < n:
        chunk = sock.recv_into(view[got:], n - got)
        if not chunk:
            raise EOFError("connection closed after %d / %d bytes" % (got, n))
        got += chunk
    return bytes(buf)


def setup_v4l2(device: str, width: int, height: int) -> int:
    """
    Open *device* for writing and configure it as a YUV420 output stream.
    Returns the file descriptor.
    """
    try:
        fd = os.open(device, os.O_WRONLY)
    except OSError as exc:
        sys.exit(
            "Cannot open %s: %s\n"
            "Make sure v4l2loopback is loaded:\n"
            "  sudo modprobe v4l2loopback devices=1 video_nr=2 "
            "card_label=droidhose exclusive_caps=1" % (device, exc)
        )

    fmt = _v4l2_format()
    fmt.type                  = V4L2_BUF_TYPE_VIDEO_OUTPUT
    fmt.fmt.pix.width         = width
    fmt.fmt.pix.height        = height
    fmt.fmt.pix.pixelformat   = V4L2_PIX_FMT_YUV420
    fmt.fmt.pix.field         = V4L2_FIELD_NONE
    fmt.fmt.pix.bytesperline  = width
    fmt.fmt.pix.sizeimage     = width * height * 3 // 2
    fmt.fmt.pix.colorspace    = V4L2_COLORSPACE_JPEG

    try:
        fcntl.ioctl(fd, VIDIOC_S_FMT, fmt)
    except OSError as exc:
        os.close(fd)
        sys.exit("VIDIOC_S_FMT failed on %s: %s" % (device, exc))

    print("v4l2loopback %s configured: %dx%d YUV420 (I420)" % (device, width, height))
    return fd


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="droidhose receiver – I420 socket → v4l2loopback",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--host",   default="localhost", metavar="HOST",
                    help="TCP host (default: localhost)")
    ap.add_argument("--port",   default=8080, type=int, metavar="PORT",
                    help="TCP port (default: 8080)")
    ap.add_argument("--device", default="/dev/video2", metavar="DEV",
                    help="v4l2loopback device (default: /dev/video2)")
    args = ap.parse_args()

    print("Connecting to %s:%d …" % (args.host, args.port))
    try:
        sock = socket.create_connection((args.host, args.port), timeout=10)
    except (OSError, socket.timeout) as exc:
        sys.exit("Connection failed: %s\nIs 'adb forward tcp:%d tcp:%d' active?" %
                 (exc, args.port, args.port))

    sock.settimeout(None)   # blocking after connect
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    print("Connected")

    # Read stream header
    try:
        hdr = recv_exact(sock, HEADER_SIZE)
    except EOFError:
        sys.exit(
            "Connected, but remote closed before sending stream header.\n"
            "This usually means the Android app is not actively streaming.\n"
            "Checks:\n"
            "  1) Launch the app and keep it in foreground once to start capture\n"
            "  2) Grant camera permission: adb shell pm grant com.droidhose android.permission.CAMERA\n"
            "  3) Verify logs: adb logcat -s droidhose\n"
            "  4) Verify port forward: adb forward --list"
        )
    if hdr[:4] != HEADER_MAGIC:
        sys.exit("Bad magic: %r (expected %r)" % (hdr[:4], HEADER_MAGIC))

    width, height = struct.unpack_from("<II", hdr, 4)
    frame_bytes   = width * height * 3 // 2
    print("Stream: %dx%d, %d bytes/frame" % (width, height, frame_bytes))

    v4l2_fd = setup_v4l2(args.device, width, height)

    frame_count = 0
    try:
        while True:
            frame = recv_exact(sock, frame_bytes)
            os.write(v4l2_fd, frame)
            frame_count += 1
            if frame_count % 60 == 0:
                print("\r%d frames written to %s" % (frame_count, args.device),
                      end="", flush=True)
    except EOFError as exc:
        print("\nStream ended: %s" % exc)
    except KeyboardInterrupt:
        print("\nStopped by user")
    finally:
        os.close(v4l2_fd)
        sock.close()
        print("\nTotal frames: %d" % frame_count)


if __name__ == "__main__":
    main()
