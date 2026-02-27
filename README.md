# droidhose

Raw, ultra-low-latency video pipe from an Android phone to a Linux PC via NDK C++.

## The Mission
Achieve the absolute minimum "Glass-to-Glass" latency by bypassing the Android MediaCodec stack entirely. Raw **YUV_420_888** frames are pulled from the **Native Camera2 NDK** and pushed directly into a TCP socket over an ADB tunnel.

## Architecture (High Performance)
To eliminate "Jitter" and "Buffer Bloat," this project uses a decoupled **Zero-Copy Producer-Consumer** model:

1.  **Camera Thread (Producer)**: Non-blocking. Performs a fast memory copy of YUV planes to a shared "mailbox" buffer and immediately returns.
2.  **Sender Thread (Consumer)**: Dedicated to network I/O. Uses a 3-buffer pointer swap to ensure it always sends the *absolute latest* frame, effectively dropping intermediate frames if the network is busy.
3.  **No RGB Conversion**: Text overlays are baked directly into the Y (Luminance) plane using a custom 5x7 alphanumeric font in C++, avoiding the massive 50ms+ penalty of ARGB conversion and Android Canvas.

## Latency Isolation & Findings
This project includes built-in diagnostic tools to isolate where time is being lost:

### On-Screen Display (OSD)
*   **ISP (ms)**: The absolute hardware processing delay. Calculated as the delta between the hardware sensor timestamp (light hitting the lens) and the moment the software receives the bytes.
    *   *Finding*: On the LG V30, this is a fixed **52ms–62ms**, representing the hardware's internal pipelining.
*   **P (Ping ID)**: Displays the latest ID received from the PC on Port 8081.

### Real-world Performance (LG V30)
Despite a 5ms network RTT and 60ms ISP delay, total "Glass-to-Glass" latency often plateaus at **~150ms**. Our research indicates this is due to deep-seated "HAL Pipelining" within certain Android vendor drivers that cannot be bypassed via public APIs.

---

## Setup & Usage

### 1. Build and Install
```bash
cd android
./gradlew installDebug
adb shell pm grant com.droidhose android.permission.CAMERA
```

### 2. Forward Ports
```bash
# Port 8080: Raw I420 Video Stream
adb forward tcp:8080 tcp:8080
# Port 8081: Latency Ping/Echo Port
adb forward tcp:8081 tcp:8081
```

### 3. Run Receiver
```bash
python3 receiver/receiver.py --device /dev/video2
```

### 4. Precision Latency Test
To measure the exact software + network overhead:
1. Send an 8-byte `int64` timestamp to `localhost:8081`.
2. The Android app will update the `P:` OSD value.
3. Observe how many milliseconds it takes for the video frame showing that ID to arrive on your PC.

---

## Technical Details

### Wire Protocol (Port 8080)
1.  **Handshake**: A 12-byte header (`DHDR` + `uint32_t width` + `uint32_t height`).
2.  **Continuous Stream**: Raw, unframed I420 bytes (`W * H * 1.5`).

### Latency Tips
*   **Use USB 3.0**: ADB performance is significantly better.
*   **CPU Priority**: The Sender thread uses `setpriority(PRIO_PROCESS, 0, -10)` to preempt background tasks.
*   **TCP_NODELAY**: Enabled on all sockets to disable Nagle's algorithm.
