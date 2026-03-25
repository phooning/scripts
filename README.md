# Scripts

Useful custom utilities across all systems I couldn't find anywhere else.

## `rffmpeg`

Recursively multi-threaded `ffmpeg` mass media conversions for audio and video. Perfect for total video/audio editing sanitization, e.g. `rffmpeg webm mp4` or `rffmpeg mp3 wav`.

- Optimized for video long-term archival flags (NVIDIA `av1_nvenc p7 -cq 10`) or Apple Silicon `VideoToolbox hevc_videotoolbox` or web/mobile delivery `h264`.

## `dl`

A parallelized and optimized `yt-dlp` wrapper with flexible options and Rich text display.

## Generate Test Fixtures

```sh
# Generic video formats.
ffmpeg -f lavfi -i color=c=black:s=64x64:r=24 -f lavfi -i anullsrc \
    -t 3 -shortest test.mp4

# HEVC
ffmpeg -f lavfi -i color=c=black:s=64x64:r=24 -f lavfi -i anullsrc \
    -c:v libx265 -t 3 -shortest test_hevc.mp4

# HEVC GPU
ffmpeg -f lavfi -i color=c=black:s=64x64:r=24 -f lavfi -i anullsrc \
    -c:v hevc_nvenc -t 3 -shortest test_hevc_gpu.mp4

# NVENC AV1
ffmpeg -f lavfi -i color=c=black:s=64x64:r=24 -f lavfi -i anullsrc \
       -c:v av1_nvenc -t 3 -shortest test_av1_gpu.mp4
```

Generate brown noise:

```sh
ffmpeg -f lavfi -i "anoisesrc=d=5:c=brown:r=48000" \
       -af "volume=0.2,aformat=sample_fmts=s32" \
       -c:a flac test_fixture.flac

ffmpeg -f lavfi -i "anoisesrc=d=5:c=brown:r=48000" \
       -af "volume=0.2" \
       -c:a alac test_fixture.m4a

ffmpeg -f lavfi -i "anoisesrc=d=5:c=brown:r=96000" \
       -af "volume=0.2" \
       -c:a pcm_f32le test_fixture.wav
```
