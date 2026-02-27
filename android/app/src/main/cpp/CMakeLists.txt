cmake_minimum_required(VERSION 3.22.1)
project(droidhose VERSION 1.0 LANGUAGES C CXX)

# ── android_native_app_glue ──────────────────────────────────────────────────
set(APP_GLUE_DIR "${ANDROID_NDK}/sources/android/native_app_glue")

add_library(native_app_glue STATIC
    "${APP_GLUE_DIR}/android_native_app_glue.c")

target_include_directories(native_app_glue PUBLIC "${APP_GLUE_DIR}")
target_link_libraries(native_app_glue log android)

# ── main shared library ───────────────────────────────────────────────────────
add_library(droidhose SHARED
    camera_stream.cpp)

target_compile_options(droidhose PRIVATE -Wall -Wextra -O2)
target_compile_features(droidhose PRIVATE cxx_std_17)

find_library(log-lib    log)
find_library(camera-lib camera2ndk)
find_library(media-lib  mediandk)
find_library(android-lib android)

target_link_libraries(droidhose
    native_app_glue
    ${log-lib}
    ${camera-lib}
    ${media-lib}
    ${android-lib})
