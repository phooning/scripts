from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

VIDEO_OUTPUTS = {"mp4", "mkv", "mov", "webm", "avi"}
AUDIO_OUTPUTS = {"mp3", "wav", "flac", "ogg", "aac", "m4a"}


def ffmpeg_has_encoder(encoder: str, ffmpeg: str = "ffmpeg") -> bool:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return any(
        line.strip().endswith(encoder) or f" {encoder}" in line
        for line in result.stdout.splitlines()
    )


def codec_is_copy_compatible(input_ext: str, output_ext: str) -> bool:
    if input_ext in {"hevc", "h265"} and output_ext in {"mkv", "mp4", "mov"}:
        return True
    if input_ext == "av1" and output_ext in {"mkv", "mp4", "webm"}:
        return True
    if input_ext in {"h264", "x264"} and output_ext in {"mkv", "mp4", "mov"}:
        return True
    return False


def resolve_flags(
    input_ext: str, output_ext: str, preset: str | None, ffmpeg: str
) -> list[str]:
    if preset == "web":
        return [
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
        ]

    if preset == "nvidia":
        if ffmpeg_has_encoder("av1_nvenc", ffmpeg):
            return [
                "-c:v",
                "av1_nvenc",
                "-preset",
                "p7",
                "-rc",
                "vbr",
                "-cq",
                "10",
                "-c:a",
                "libopus",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
            ]
        return [
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
        ]

    if preset == "apple":
        if ffmpeg_has_encoder("hevc_videotoolbox", ffmpeg):
            return [
                "-c:v",
                "hevc_videotoolbox",
                "-q:v",
                "60",
                "-tag:v",
                "hvc1",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
            ]
        return [
            "-c:v",
            "libx265",
            "-preset",
            "slow",
            "-crf",
            "24",
            "-tag:v",
            "hvc1",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
        ]

    pair = f"{input_ext}->{output_ext}"

    match pair:
        case (
            "webm->mp4"
            | "mkv->mp4"
            | "avi->mp4"
            | "mov->mp4"
            | "hevc->mp4"
            | "h265->mp4"
            | "av1->mp4"
        ):
            return [
                "-c:v",
                "libx264",
                "-preset",
                "slow",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
            ]

        case "mp4->mkv" | "webm->mkv" | "avi->mkv" | "mov->mkv" | "av1->mkv":
            return [
                "-c:v",
                "libx265",
                "-preset",
                "slow",
                "-crf",
                "24",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
            ]

        case "hevc->mkv" | "h265->mkv":
            return ["-c:v", "copy", "-c:a", "copy"]

        case "hevc->mp4" | "h265->mp4":
            return ["-c:v", "copy", "-c:a", "copy", "-movflags", "+faststart"]

        case "mp4->webm" | "mkv->webm" | "mov->webm":
            return [
                "-c:v",
                "libsvtav1",
                "-crf",
                "30",
                "-c:a",
                "libopus",
                "-b:a",
                "128k",
            ]

        case "av1->webm" | "av1->mkv" | "av1->mp4":
            return ["-c:v", "copy", "-c:a", "copy"]

        case "mp4->gif" | "webm->gif" | "mkv->gif":
            return ["-vf", "fps=15,scale=480:-1:flags=lanczos"]

        case "mp3->wav" | "flac->wav" | "ogg->wav" | "m4a->wav":
            return ["-c:a", "pcm_s16le"]

        case "wav->mp3" | "flac->mp3" | "ogg->mp3" | "m4a->mp3":
            return ["-c:a", "libmp3lame", "-q:a", "2"]

        case "wav->flac" | "mp3->flac" | "ogg->flac" | "m4a->flac":
            return ["-c:a", "flac"]

    if codec_is_copy_compatible(input_ext, output_ext):
        return ["-c:v", "copy", "-c:a", "copy"]

    if output_ext in AUDIO_OUTPUTS:
        return ["-vn"]

    if output_ext in VIDEO_OUTPUTS:
        return ["-c:v", "libx264", "-c:a", "aac"]

    return []


def discover_files(root: Path, input_ext: str) -> list[Path]:
    suffix = f".{input_ext.lower()}"
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() == suffix
    ]


def output_path_for(
    input_file: Path, input_ext: str, output_ext: str, out_dir: Path | None, root: Path
) -> Path:
    if out_dir is None:
        return input_file.with_suffix(f".{output_ext}")

    relative = input_file.relative_to(root)
    return (out_dir / relative).with_suffix(f".{output_ext}")


def convert_one(
    ffmpeg: str, input_file: Path, output_file: Path, flags: list[str], dry_run: bool
) -> tuple[bool, Path, str]:
    output_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-stats",
        "-i",
        str(input_file),
        *flags,
        str(output_file),
    ]

    if dry_run:
        return True, input_file, " ".join(cmd)

    result = subprocess.run(cmd, check=False)
    return result.returncode == 0, input_file, ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recursively convert media files using ffmpeg."
    )
    parser.add_argument("input_ext")
    parser.add_argument("output_ext")
    parser.add_argument("-p", "--preset", choices=["web", "nvidia", "apple"])
    parser.add_argument(
        "-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 4) // 2)
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    input_ext = args.input_ext.lower().lstrip(".")
    output_ext = args.output_ext.lower().lstrip(".")

    flags = resolve_flags(input_ext, output_ext, args.preset, args.ffmpeg)
    files = discover_files(args.root, input_ext)

    jobs = []
    skipped = 0

    for input_file in files:
        output_file = output_path_for(
            input_file, input_ext, output_ext, args.out_dir, args.root
        )

        if output_file.exists() and not args.overwrite:
            skipped += 1
            continue

        jobs.append((input_file, output_file))

    print("=" * 78)
    print(f"Preset: {args.preset or 'none'}")
    print(f"Root: {args.root}")
    print(f"Convert: .{input_ext} -> .{output_ext}")
    print(f"Flags: {' '.join(flags)}")
    print(f"Found: {len(files)}")
    print(f"Skipped: {skipped}")
    print(f"Queued: {len(jobs)}")
    print(f"Jobs: {args.jobs}")
    print("=" * 78)

    if not jobs:
        print("No files to process.")
        return 0

    success = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = [
            executor.submit(
                convert_one, args.ffmpeg, input_file, output_file, flags, args.dry_run
            )
            for input_file, output_file in jobs
        ]

        for future in as_completed(futures):
            ok, input_file, message = future.result()

            if args.dry_run:
                print(f"[DRY] {message}")
                success += 1
            elif ok:
                print(f"[OK] {input_file}")
                success += 1
            else:
                print(f"[FAIL] {input_file}")
                failed += 1

    print("=" * 78)
    print(f"Success: {success}")
    print(f"Failed: {failed}")
    print(f"Skipped: {skipped}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
