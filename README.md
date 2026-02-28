# Scripts

Useful custom utilities across all systems I couldn't find anywhere else.

`rffmpeg`: recursively multi-threaded `ffmpeg` mass media conversions for audio and video. Perfect for total video/audio editing sanitization, e.g. `rffmpeg webm mp4` or `rffmpeg mp3 wav`.

- Optimized for video long-term archival flags (NVIDIA `av1_nvenc p7 -cq 10`) or Apple Silicon `VideoToolbox hevc_videotoolbox` or web/mobile delivery `h264`.
