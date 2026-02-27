/*
 * droidhose – camera_stream.cpp
 *
 * Android NDK NativeActivity that opens the back camera via the Camera2 NDK
 * API, pulls raw YUV_420_888 frames and streams them as packed I420 over a
 * plain TCP socket on port 8080.
 *
 * Wire protocol
 * ─────────────
 * On every new connection the sender sends a 12-byte header once:
 *
 *   Bytes  0-3  : magic "DHDR"
 *   Bytes  4-7  : width  (uint32_t, little-endian)
 *   Bytes  8-11 : height (uint32_t, little-endian)
 *
 * Followed by a continuous stream of packed I420 frames:
 *
 *   Y plane : width × height bytes
 *   U plane : (width/2) × (height/2) bytes
 *   V plane : (width/2) × (height/2) bytes
 *
 * ADB tunnel (run on the PC):
 *   adb forward tcp:8080 tcp:8080
 */

#include <android/log.h>
#include <android_native_app_glue.h>
#include <camera/NdkCameraDevice.h>
#include <camera/NdkCameraManager.h>
#include <camera/NdkCameraCaptureSession.h>
#include <media/NdkImage.h>
#include <media/NdkImageReader.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* ── logging ─────────────────────────────────────────────────────────────── */
#define TAG  "droidhose"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO,  TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, TAG, __VA_ARGS__)

/* ── tunables ────────────────────────────────────────────────────────────── */
#define SERVER_PORT   8080
#define FRAME_W       640
#define FRAME_H       480
#define MAX_IMAGES    4         /* AImageReader queue depth                   */
#define CAM_ID_LEN    64

#define FRAME_BYTES   ((size_t)(FRAME_W) * (FRAME_H) * 3 / 2)

/* ── stream header ───────────────────────────────────────────────────────── */
static const uint8_t HDR_MAGIC[4] = {'D', 'H', 'D', 'R'};

/* ── client-fd guarded by a mutex ────────────────────────────────────────── */
static pthread_mutex_t g_fd_lock  = PTHREAD_MUTEX_INITIALIZER;
static int             g_client_fd = -1;

static void set_client(int fd)
{
    pthread_mutex_lock(&g_fd_lock);
    if (g_client_fd >= 0) {
        close(g_client_fd);
    }
    g_client_fd = fd;
    pthread_mutex_unlock(&g_fd_lock);
}

static int get_client(void)
{
    pthread_mutex_lock(&g_fd_lock);
    int fd = g_client_fd;
    pthread_mutex_unlock(&g_fd_lock);
    return fd;
}

static void drop_client(void)
{
    pthread_mutex_lock(&g_fd_lock);
    if (g_client_fd >= 0) {
        close(g_client_fd);
        g_client_fd = -1;
    }
    pthread_mutex_unlock(&g_fd_lock);
}

/* ── write helper (handles short writes) ────────────────────────────────── */
static int write_all(int fd, const void *buf, size_t len)
{
    const uint8_t *p = (const uint8_t *)buf;
    size_t off = 0;
    while (off < len) {
        ssize_t n = write(fd, p + off, len - off);
        if (n <= 0) return -1;
        off += (size_t)n;
    }
    return 0;
}

/* ── packed frame buffer (heap-allocated in camera_start) ───────────────── */
static uint8_t *g_frame_buf = NULL;

/* ── image callback – fires on the camera's internal thread ─────────────── */
static void on_image_available(void *ctx, AImageReader *reader)
{
    (void)ctx;

    AImage *img = NULL;
    if (AImageReader_acquireLatestImage(reader, &img) != AMEDIA_OK || !img)
        return;

    /* Plane data pointers & metadata */
    uint8_t *yp = NULL, *up = NULL, *vp = NULL;
    int      yl = 0,    ul = 0,    vl = 0;
    int32_t  ys = 0,    us = 0,    vs = 0;   /* row strides        */
    int32_t  ups = 0,   vps = 0;             /* chroma pixel strides */

    AImage_getPlaneData(img, 0, &yp, &yl);
    AImage_getPlaneData(img, 1, &up, &ul);
    AImage_getPlaneData(img, 2, &vp, &vl);
    AImage_getPlaneRowStride(img, 0, &ys);
    AImage_getPlaneRowStride(img, 1, &us);
    AImage_getPlaneRowStride(img, 2, &vs);
    AImage_getPlanePixelStride(img, 1, &ups);
    AImage_getPlanePixelStride(img, 2, &vps);

    const int W = FRAME_W, H = FRAME_H;

    /* ── Y plane: strip row-stride padding ─────────────────────────────── */
    for (int r = 0; r < H; r++)
        memcpy(g_frame_buf + r * W, yp + r * ys, (size_t)W);

    /* ── U plane ────────────────────────────────────────────────────────── */
    uint8_t *ud = g_frame_buf + W * H;
    for (int r = 0; r < H / 2; r++)
        for (int c = 0; c < W / 2; c++)
            ud[r * (W / 2) + c] = up[r * us + c * ups];

    /* ── V plane ────────────────────────────────────────────────────────── */
    uint8_t *vd = g_frame_buf + W * H + (W / 2) * (H / 2);
    for (int r = 0; r < H / 2; r++)
        for (int c = 0; c < W / 2; c++)
            vd[r * (W / 2) + c] = vp[r * vs + c * vps];

    AImage_delete(img);

    /* ── send to connected client ────────────────────────────────────────── */
    int fd = get_client();
    if (fd < 0) return;

    if (write_all(fd, g_frame_buf, FRAME_BYTES) < 0) {
        LOGE("send failed (%s); dropping client", strerror(errno));
        drop_client();
    }
}

/* ── camera device callbacks ─────────────────────────────────────────────── */
static void cam_disconnected(void *ctx, ACameraDevice *dev)
{
    (void)ctx; (void)dev;
    LOGI("Camera disconnected");
}

static void cam_error(void *ctx, ACameraDevice *dev, int err)
{
    (void)ctx; (void)dev;
    LOGE("Camera error %d", err);
}

static ACameraDevice_StateCallbacks g_cam_cbs = {
    .context        = NULL,
    .onDisconnected = cam_disconnected,
    .onError        = cam_error,
};

/* ── capture-session state callbacks ─────────────────────────────────────── */
static void sess_closed(void *ctx, ACameraCaptureSession *s) { (void)ctx; (void)s; }
static void sess_ready (void *ctx, ACameraCaptureSession *s) { (void)ctx; (void)s; }
static void sess_active(void *ctx, ACameraCaptureSession *s)
{
    (void)ctx; (void)s;
    LOGI("Capture session active – streaming %dx%d I420 on :%d",
         FRAME_W, FRAME_H, SERVER_PORT);
}

static ACameraCaptureSession_stateCallbacks g_sess_cbs = {
    .context  = NULL,
    .onClosed = sess_closed,
    .onReady  = sess_ready,
    .onActive = sess_active,
};

/* ── camera handles (kept alive for the process lifetime) ────────────────── */
static ACameraManager                   *g_mgr  = NULL;
static ACameraDevice                    *g_dev  = NULL;
static AImageReader                     *g_rdr  = NULL;
static ACaptureRequest                  *g_req  = NULL;
static ACameraCaptureSession            *g_sess = NULL;
static ACaptureSessionOutputContainer  *g_cont = NULL;
static ACaptureSessionOutput            *g_sout = NULL;
static ACameraOutputTarget              *g_tgt  = NULL;

/* ── camera_start: enumerate → open → image-reader → session ────────────── */
static int camera_start(void)
{
    g_frame_buf = (uint8_t *)malloc(FRAME_BYTES);
    if (!g_frame_buf) { LOGE("malloc frame buf failed"); return -1; }

    g_mgr = ACameraManager_create();
    if (!g_mgr) { LOGE("ACameraManager_create failed"); return -1; }

    /* Enumerate and pick the back-facing camera */
    ACameraIdList *ids = NULL;
    if (ACameraManager_getCameraIdList(g_mgr, &ids) != ACAMERA_OK
            || !ids || ids->numCameras == 0) {
        LOGE("No cameras available");
        return -1;
    }

    static char cam_id[CAM_ID_LEN];
    strncpy(cam_id, ids->cameraIds[0], CAM_ID_LEN - 1);   /* default: first */
    cam_id[CAM_ID_LEN - 1] = '\0';

    for (int i = 0; i < ids->numCameras; i++) {
        ACameraMetadata *meta = NULL;
        ACameraManager_getCameraCharacteristics(g_mgr, ids->cameraIds[i], &meta);
        ACameraMetadata_const_entry e = {};
        if (ACameraMetadata_getConstEntry(meta, ACAMERA_LENS_FACING, &e) == ACAMERA_OK
                && e.data.u8[0] == ACAMERA_LENS_FACING_BACK) {
            strncpy(cam_id, ids->cameraIds[i], CAM_ID_LEN - 1);
            cam_id[CAM_ID_LEN - 1] = '\0';
            ACameraMetadata_free(meta);
            break;
        }
        ACameraMetadata_free(meta);
    }

    camera_status_t cs = ACameraManager_openCamera(g_mgr, cam_id,
                                                    &g_cam_cbs, &g_dev);
    ACameraManager_freeCameraIdList(ids);
    if (cs != ACAMERA_OK) { LOGE("openCamera(%s) failed: %d", cam_id, cs); return -1; }
    LOGI("Opened camera %s", cam_id);

    /* Image reader: YUV_420_888, 640×480, queue depth MAX_IMAGES */
    if (AImageReader_new(FRAME_W, FRAME_H, AIMAGE_FORMAT_YUV_420_888,
                         MAX_IMAGES, &g_rdr) != AMEDIA_OK) {
        LOGE("AImageReader_new failed");
        return -1;
    }
    AImageReader_ImageListener il = { .context = NULL, .onImageAvailable = on_image_available };
    AImageReader_setImageListener(g_rdr, &il);

    ANativeWindow *win = NULL;
    AImageReader_getWindow(g_rdr, &win);

    /* Build capture session */
    ACaptureSessionOutputContainer_create(&g_cont);
    ACaptureSessionOutput_create(win, &g_sout);
    ACaptureSessionOutputContainer_add(g_cont, g_sout);
    ACameraOutputTarget_create(win, &g_tgt);

    cs = ACameraDevice_createCaptureSession(g_dev, g_cont, &g_sess_cbs, &g_sess);
    if (cs != ACAMERA_OK) { LOGE("createCaptureSession failed: %d", cs); return -1; }

    cs = ACameraDevice_createCaptureRequest(g_dev, TEMPLATE_RECORD, &g_req);
    if (cs != ACAMERA_OK) { LOGE("createCaptureRequest failed: %d", cs); return -1; }
    ACaptureRequest_addTarget(g_req, g_tgt);

    cs = ACameraCaptureSession_setRepeatingRequest(g_sess, NULL, 1, &g_req, NULL);
    if (cs != ACAMERA_OK) { LOGE("setRepeatingRequest failed: %d", cs); return -1; }

    return 0;
}

/* ── TCP accept loop (runs in a detached thread) ─────────────────────────── */
static void *accept_thread(void *arg)
{
    (void)arg;

    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) { LOGE("socket: %s", strerror(errno)); return NULL; }

    int one = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family      = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port        = htons(SERVER_PORT);

    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        LOGE("bind: %s", strerror(errno));
        close(srv);
        return NULL;
    }
    listen(srv, 1);
    LOGI("TCP server listening on :%d", SERVER_PORT);

    for (;;) {
        int fd = accept(srv, NULL, NULL);
        if (fd < 0) continue;
        LOGI("New client connected");

        /* Send 12-byte stream header */
        uint8_t hdr[12];
        memcpy(hdr, HDR_MAGIC, 4);
        uint32_t w = FRAME_W, h = FRAME_H;
        memcpy(hdr + 4, &w, 4);
        memcpy(hdr + 8, &h, 4);

        if (write_all(fd, hdr, sizeof(hdr)) == 0) {
            set_client(fd);
        } else {
            LOGE("Failed to send stream header");
            close(fd);
        }
    }

    close(srv);   /* unreachable but tidy */
    return NULL;
}

/* ── NativeActivity entry point ──────────────────────────────────────────── */
void android_main(struct android_app *app)
{
    /* Start accept thread before camera so clients can connect early */
    pthread_t t;
    if (pthread_create(&t, NULL, accept_thread, NULL) == 0)
        pthread_detach(t);

    if (camera_start() < 0) {
        LOGE("camera_start failed – exiting");
        return;
    }

    /* Main event loop – keeps the NativeActivity alive */
    for (;;) {
        int events;
        struct android_poll_source *src;
        ALooper_pollAll(500 /*ms*/, NULL, &events, (void **)&src);
        if (src) src->process(app, src);
        if (app->destroyRequested) {
            LOGI("Destroy requested – stopping");
            break;
        }
    }

    /* Orderly teardown */
    drop_client();
    if (g_sess)  ACameraCaptureSession_close(g_sess);
    if (g_req)   ACaptureRequest_free(g_req);
    if (g_tgt)   ACameraOutputTarget_free(g_tgt);
    if (g_sout)  ACaptureSessionOutput_free(g_sout);
    if (g_cont)  ACaptureSessionOutputContainer_free(g_cont);
    if (g_rdr)   AImageReader_delete(g_rdr);
    if (g_dev)   ACameraDevice_close(g_dev);
    if (g_mgr)   ACameraManager_delete(g_mgr);
    free(g_frame_buf);
}
