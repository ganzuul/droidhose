# droidhose

Raw, ultra-low-latency video pipe from an Android phone to a Linux PC.

Achieves **~20 ms glass-to-glass latency** by bypassing Android's hardware
encoder entirely: the camera ISP hands off raw **YUV_420_888** frames
directly into a TCP socket over a USB 3 ADB tunnel.  The Linux side writes
those frames straight into a **v4l2loopback** device so any V4L2 application
(OpenTrack, OBS, ffplay …) sees the phone as a regular webcam.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Android (NDK C++)                                  │
│                                                     │
│  Camera ISP  ──►  AImageReader (YUV_420_888)        │
│                        │ image callback              │
│                        ▼                            │
│               pack to I420 in RAM                   │
│                        │                            │
│                        ▼                            │
│               TCP socket :8080                      │
└─────────────────────────────────────────────────────┘
              │  USB 3.x (5 Gbps)  │
              │  adb forward       │
┌─────────────▼─────────────────────────────────────┐
│  Linux (Python)                                   │
│                                                   │
│  localhost:8080  ──►  /dev/video2 (v4l2loopback)  │
└───────────────────────────────────────────────────┘
```

**Latency breakdown** (640×480 @ 60 fps over USB 3.0):

| Step                    | Time  |
|-------------------------|-------|
| Sensor → RAM (ISP)      | ~10 ms |
| RAM → TCP socket (C++)  | ~2 ms  |
| USB 3.0 ADB transfer    | ~2 ms  |
| Socket → v4l2loopback   | ~2 ms  |
| **Total**               | **~16–20 ms** |

---

## Building the Android app

### Requirements

* Android Studio Hedgehog (2023.1) or newer
* Android NDK **r25c** or newer (install via SDK Manager → SDK Tools → NDK)
* A physical Android device running API 26+ (Android 8.0+)
* USB debugging enabled on the device

### Steps

1. Open the `android/` directory as an Android Studio project
   (`File → Open → select the android/ folder`).

2. Sync Gradle (Android Studio will prompt automatically).

3. Build and install:
   ```bash
   # From the android/ directory
   ./gradlew installDebug
   ```
   Or use the **Run** button in Android Studio.

4. Grant the CAMERA permission (required once):
   ```bash
   adb shell pm grant com.droidhose android.permission.CAMERA
   ```

5. Launch the app from the phone's launcher (it shows a blank screen —
   all work happens natively in the background).  You should see in logcat:
   ```
   droidhose: TCP server listening on :8080
   droidhose: Capture session active – streaming 640x480 I420 on :8080
   ```

---

## Setting up the Linux side

### 1. Load v4l2loopback (once per boot)

```bash
sudo modprobe v4l2loopback devices=1 video_nr=2 \
    card_label=droidhose exclusive_caps=1
```

To make it permanent, add to `/etc/modules-load.d/v4l2loopback.conf`:
```
v4l2loopback
```
And to `/etc/modprobe.d/v4l2loopback.conf`:
```
options v4l2loopback devices=1 video_nr=2 card_label=droidhose exclusive_caps=1
```

### 2. Forward the ADB port

```bash
adb forward tcp:8080 tcp:8080
```

### 3. Run the receiver

```bash
python3 receiver/receiver.py
# Optional flags:
#   --host localhost   (default)
#   --port 8080        (default)
#   --device /dev/video2  (default)
```

### 4. Verify

```bash
ffplay -f v4l2 /dev/video2
# or
v4l2-ctl --list-devices
```

---

## Wire protocol

The sender (Android) emits a **12-byte header** once per connection:

| Offset | Size | Description                  |
|--------|------|------------------------------|
| 0      | 4    | Magic `DHDR` (ASCII)         |
| 4      | 4    | Frame width  (uint32_t LE)   |
| 8      | 4    | Frame height (uint32_t LE)   |

Followed by a continuous stream of unframed **I420** (planar YUV 4:2:0) data:

```
Y plane : width × height bytes
U plane : (width/2) × (height/2) bytes
V plane : (width/2) × (height/2) bytes
```

No per-frame size headers are needed because the frame size is fixed once the
header has been read.

---

## Adjusting resolution / frame rate

Edit the constants at the top of
`android/app/src/main/cpp/camera_stream.cpp`:

```c
#define SERVER_PORT   8080
#define FRAME_W       640
#define FRAME_H       480
#define MAX_IMAGES    4     // AImageReader queue depth
```

Verify your device supports the chosen resolution in YUV_420_888 mode:
```bash
adb shell dumpsys media.camera | grep -A5 "JPEG\|YUV"
```

---

## Bandwidth guide

| Resolution | FPS | Raw YUV bandwidth |
|------------|-----|-------------------|
| 640×480    | 60  | ~220 Mbps         |
| 1280×720   | 30  | ~330 Mbps         |
| 1920×1080  | 30  | ~746 Mbps         |

All comfortably within USB 3.x (5 Gbps) capacity over ADB.
