#!/usr/bin/env python3
"""
dl.py — Parallel yt-dlp downloader with archive deduplication and live Rich UI.

Usage:
    python dl.py [URL ...]
    python dl.py --file urls.txt
    python dl.py --workers 4 --archive archive.txt URL1 URL2
    python dl.py --audio-only URL
    python dl.py --format "bestvideo+bestaudio/best" URL

Dependencies:
    pip install yt-dlp rich
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── yt-dlp invocation / self-update ──────────────────────────────────────────

# Always use the yt-dlp installed alongside this script (the one that
# update_ytdlp() upgrades), not whatever happens to be first on PATH.
YTDLP: list[str] = [sys.executable, "-m", "yt_dlp"]


def update_ytdlp() -> None:
    """Upgrade yt-dlp in this interpreter's environment. Failures are non-fatal."""
    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "--quiet", "--upgrade",
               "--python", sys.executable, "yt-dlp"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "yt-dlp"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            print(f"[warn] yt-dlp update failed: {r.stderr.strip()[-200:]}", file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[warn] yt-dlp update failed: {exc}", file=sys.stderr)

# ── Status constants ──────────────────────────────────────────────────────────

STATUS_WAITING    = "waiting"
STATUS_RESOLVING  = "resolving"
STATUS_FETCHING   = "fetching"
STATUS_DOWNLOAD   = "downloading"
STATUS_MERGE      = "merging"
STATUS_DONE       = "done"
STATUS_SKIPPED    = "skipped"
STATUS_ERROR      = "error"

STATUS_STYLE: dict[str, tuple[str, str]] = {
    STATUS_WAITING:   ("dim",          "⏳"),
    STATUS_RESOLVING: ("yellow",       "🔍"),
    STATUS_FETCHING:  ("bright_yellow","📡"),
    STATUS_DOWNLOAD:  ("cyan",         "⬇ "),
    STATUS_MERGE:     ("magenta",      "🔀"),
    STATUS_DONE:      ("bold green",   "✅"),
    STATUS_SKIPPED:   ("dim green",    "⏭ "),
    STATUS_ERROR:     ("bold red",     "❌"),
}

# ── Regex patterns ────────────────────────────────────────────────────────────

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
_SPEED_RE   = re.compile(r"(\d+(?:\.\d+)?(?:K|M|G)iB/s)")
_ETA_RE     = re.compile(r"ETA\s+(\S+)")
_DEST_RE    = re.compile(r"\[download\] Destination: (.+)")
_TITLE_RE   = re.compile(r"\[(?:youtube|soundcloud|vimeo|info)\].*?: (.+)")
_MERGE_RE   = re.compile(r"\[Merger\]|Merging")
_FMT_ROW_RE = re.compile(r"^(\S+)\s")
_AUDIO_TBR_RE = re.compile(r"\s(\d+(?:\.\d+)?)k\s+https\b")

# ── State ─────────────────────────────────────────────────────────────────────

@dataclass
class DownloadState:
    url: str
    status: str   = STATUS_WAITING
    title: str    = ""
    percent: float = 0.0
    speed: str    = ""
    eta: str      = ""
    error: str    = ""
    dest: str     = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

# ── yt-dlp output parser ──────────────────────────────────────────────────────

def parse_ytdlp_line(line: str, state: DownloadState) -> None:
    """Update DownloadState from a single yt-dlp --newline output line."""
    with state.lock:
        if line.startswith("DLPATH:"):
            state.dest = line[len("DLPATH:"):]
            return

        if _MERGE_RE.search(line):
            state.status = STATUS_MERGE
            return

        if "[download]" in line:
            m_pct = _PERCENT_RE.search(line)
            if m_pct:
                state.status = STATUS_DOWNLOAD
                state.percent = float(m_pct.group(1))
            m_spd = _SPEED_RE.search(line)
            if m_spd:
                state.speed = m_spd.group(1)
            m_eta = _ETA_RE.search(line)
            if m_eta:
                state.eta = m_eta.group(1)
            m_dst = _DEST_RE.match(line)
            if m_dst and not state.title:
                state.title = Path(m_dst.group(1)).stem[:60]
            return

        if not state.title:
            m_ttl = _TITLE_RE.search(line)
            if m_ttl:
                state.title = m_ttl.group(1)[:60]
                state.status = STATUS_FETCHING

# ── Archive helpers ───────────────────────────────────────────────────────────

archive_lock = threading.Lock()


def load_archive(path: Optional[Path]) -> set[str]:
    if path is None or not path.exists():
        return set()
    return {l.strip() for l in path.read_text().splitlines() if l.strip()}


def get_archive_key(url: str) -> Optional[str]:
    """
    Ask yt-dlp for the archive key (extractor + id) without downloading.
    Returns e.g. 'youtube dQw4w9WgXcQ', or None on failure.
    """
    try:
        out = subprocess.check_output(
            [
                *YTDLP,
                "--skip-download",
                "--print", "%(extractor)s %(id)s",
                "--no-warnings",
                "--quiet",
                url,
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        ).strip()
        return out if " " in out else None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def resolve_state(
    state: DownloadState,
    downloaded: set[str],
) -> None:
    """
    Set state to SKIPPED if already in archive, WAITING if new, or leave
    as WAITING if key could not be resolved (will attempt download anyway).
    Mutates state.status in-place.
    """
    with state.lock:
        state.status = STATUS_RESOLVING
        state.title  = "resolving…"

    key = get_archive_key(state.url)

    with state.lock:
        state.title = ""
        if key is not None and key in downloaded:
            state.status = STATUS_SKIPPED
            state.percent = 100.0
            state.title   = "(already downloaded)"
        else:
            state.status = STATUS_WAITING  # ready to queue

# ── Format auto-select ────────────────────────────────────────────────────────

def _last_fmt(lines: list[str], kind: str) -> Optional[str]:
    last = None
    for line in lines:
        if kind.lower() in line.lower():
            m = _FMT_ROW_RE.match(line.strip())
            if m:
                last = m.group(1)
    return last


def _any_fmt(lines: list[str]) -> Optional[str]:
    last = None
    for line in lines:
        s = line.strip()
        if not s or s.startswith("ID") or set(s).issubset({"-", "+", "|", " "}):
            continue
        m = _FMT_ROW_RE.match(s)
        if m and m.group(1) not in ("ID", "["):
            last = m.group(1)
    return last


def resolve_format(url: str, state: DownloadState) -> Optional[str]:
    """
    Run `yt-dlp -F` and return a composite format string, or None (fallback).
    State title is updated while querying so the UI stays informative.
    """
    with state.lock:
        state.status = STATUS_FETCHING
        state.title  = "querying formats…"

    try:
        result = subprocess.run(
            [*YTDLP, "-F", url],
            capture_output=True, text=True, timeout=60,
        )
        lines      = result.stdout.splitlines()
        last_video = _last_fmt(lines, "video only")
        last_audio = _last_fmt(lines, "audio only")
        if last_video and last_audio:
            return f"{last_video}+{last_audio}"
        return _any_fmt(lines)
    except Exception:
        return None

def resolve_audio_format(url: str, state: DownloadState, proxy: str) -> str:
    """
    Pick the best direct (https) audio-only format ID from `yt-dlp -F`
    (e.g. 251 for opus webm). Falls back to yt-dlp's "bestaudio/best".

    No player_client override is used: forcing the `web` client can expose
    no audio formats at all for some videos.
    """
    with state.lock:
        state.status = STATUS_FETCHING
        state.title  = "querying formats…"
    cmd = [*YTDLP, "--ignore-config", "--no-warnings", "-F", url]
    if proxy:
        cmd += ["--proxy", proxy]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return "bestaudio/best"
    best: Optional[tuple[float, str]] = None
    for line in out.splitlines():
        if "audio only" not in line:
            continue
        m_id, m_br = _FMT_ROW_RE.match(line.strip()), _AUDIO_TBR_RE.search(line)
        if m_id and m_br:
            cand = (float(m_br.group(1)), m_id.group(1))
            if best is None or cand[0] > best[0]:
                best = cand
    return best[1] if best else "bestaudio/best"


def convert_to_mp4(src: Path, state: DownloadState) -> None:
    """ffmpeg the downloaded audio stream into an AAC .mp4; remove source."""
    dst = src.with_suffix(".mp4")
    with state.lock:
        state.status = STATUS_MERGE
    if src == dst:
        return
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-vn", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[-80:]}")
    src.unlink(missing_ok=True)
    with state.lock:
        state.dest = str(dst)


# ── Downloader ────────────────────────────────────────────────────────────────

def download(
    state:       DownloadState,
    output_dir:  str,
    extra_args:  list[str],
    auto_format: bool,
    archive_path: Optional[Path],
    proxy:       str,
    audio_only:  bool = False,
) -> None:
    """Download one URL, updating state live; append to archive on success."""

    # Skip items pre-filtered as already downloaded
    with state.lock:
        if state.status == STATUS_SKIPPED:
            return

    # ── Optional: auto-select best format via yt-dlp -F ──────────────────────
    fmt_args: list[str] = []
    if audio_only:
        fmt_args = ["-f", resolve_audio_format(state.url, state, proxy)]
    elif auto_format:
        fmt = resolve_format(state.url, state)
        if fmt:
            fmt_args = ["-f", fmt]

    # ── Use a temp archive so we can do an atomic append on success ───────────
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
    tmp.close()

    cmd = [
        *YTDLP,
        "--newline",
        "-o", f"{output_dir}/%(title)s.%(ext)s",
        "--download-archive", tmp.name,
        "--sleep-interval",   "3",
        "--max-sleep-interval","8",
        "--retries",          "10",
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "--no-warnings",
    ]
    if audio_only:
        # Raw stream download only; conversion is done with ffmpeg afterwards.
        # Ignore user config (it may embed subs / remux to mkv).
        cmd += ["--ignore-config", "--no-quiet", "--no-embed-subs",
                "--no-write-subs", "--no-embed-metadata", "--no-sponsorblock",
                "--print", "after_move:DLPATH:%(filepath)s"]
    else:
        cmd += ["--extractor-args", "youtube:player_client=web"]
    cmd += [*fmt_args, *extra_args]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(state.url)

    with state.lock:
        state.status = STATUS_FETCHING
        if not state.title or state.title in ("querying formats…", "resolving…"):
            state.title = ""

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if proc.stdout is None:
            raise RuntimeError("Failed to open yt-dlp stdout pipe")

        for line in proc.stdout:
            parse_ytdlp_line(line.rstrip(), state)

        proc.wait()

        if proc.returncode == 0 and audio_only:
            if not state.dest:
                raise RuntimeError("could not determine downloaded file")
            convert_to_mp4(Path(state.dest), state)

        with state.lock:
            if proc.returncode == 0:
                state.status  = STATUS_DONE
                state.percent = 100.0
            else:
                state.status = STATUS_ERROR
                state.error  = f"exit code {proc.returncode}"

        # Atomic archive append on success
        if proc.returncode == 0 and archive_path is not None:
            tmp_lines = Path(tmp.name).read_text().splitlines()
            if tmp_lines:
                with archive_lock:
                    with archive_path.open("a") as f:
                        f.write("\n".join(tmp_lines) + "\n")

    except FileNotFoundError:
        with state.lock:
            state.status = STATUS_ERROR
            state.error  = "yt-dlp not found — run: uv tool install --reinstall dl"
    except Exception as exc:
        with state.lock:
            state.status = STATUS_ERROR
            state.error  = str(exc)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

# ── Rich UI ───────────────────────────────────────────────────────────────────

def build_table(states: list[DownloadState]) -> Table:
    table = Table(
        box=box.ROUNDED,
        expand=True,
        show_header=True,
        header_style="bold bright_white on grey23",
        border_style="grey42",
    )
    table.add_column("#",        style="dim",          width=3,  no_wrap=True)
    table.add_column("Status",   style="",             width=14, no_wrap=True)
    table.add_column("Title / URL",                    ratio=3,  no_wrap=True)
    table.add_column("Progress",                       width=22, no_wrap=True)
    table.add_column("Speed",    style="cyan",         width=12, no_wrap=True)
    table.add_column("ETA",      style="bright_black", width=7,  no_wrap=True)

    for i, s in enumerate(states, 1):
        with s.lock:
            style, icon = STATUS_STYLE.get(s.status, ("", "?"))
            label = Text(f"{icon} {s.status}", style=style)

            display = s.title if s.title else s.url
            if len(display) > 55:
                display = display[:52] + "…"

            filled  = int(s.percent / 100 * 20)
            bar_str = f"[{'█' * filled}{'░' * (20 - filled)}] {s.percent:5.1f}%"

            if s.status == STATUS_SKIPPED:
                bar_text = Text("already downloaded", style="dim green")
            elif s.status == STATUS_ERROR:
                bar_text = Text(s.error[:24], style="red")
            else:
                bar_text = Text(
                    bar_str,
                    style="cyan" if s.status == STATUS_DOWNLOAD else "dim",
                )

            table.add_row(
                str(i),
                label,
                display,
                bar_text,
                s.speed or "—",
                s.eta   or "—",
            )

    return table

# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parallel yt-dlp downloader with archive deduplication and live UI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("urls",      nargs="*", help="Media URLs to download")
    parser.add_argument("--file",    "-f", metavar="FILE",
                        help="Text file with one URL per line")
    parser.add_argument("--workers", "-w", type=int, default=3, metavar="N",
                        help="Max parallel downloads (default: 3)")
    parser.add_argument("--resolve-workers", type=int, default=8, metavar="N",
                        help="Workers for parallel archive pre-resolution (default: 8)")
    parser.add_argument("--output",  "-o", default="downloads", metavar="DIR",
                        help="Output directory (default: ./downloads)")
    parser.add_argument("--archive", metavar="FILE",
                        help="yt-dlp archive file for skip-already-downloaded logic")
    parser.add_argument("--format",  metavar="FMT",
                        help="yt-dlp format string (e.g. 'bestvideo+bestaudio/best')")
    parser.add_argument("--audio-only", "-a", action="store_true",
                        help="Download best audio-only stream and convert to AAC .mp4 via ffmpeg")
    parser.add_argument("--no-update", action="store_true",
                        help="Skip the automatic yt-dlp upgrade at startup")
    parser.add_argument("--proxy",   metavar="URL", default="",
                        help="Proxy URL (e.g. socks5://127.0.0.1:9050)")
    args, extra = parser.parse_known_args()

    # ── Collect URLs ──────────────────────────────────────────────────────────
    urls: list[str] = list(args.urls)
    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"[error] File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        urls += [
            u.strip()
            for u in path.read_text().splitlines()
            if u.strip() and not u.startswith("#")
        ]

    if not urls:
        parser.print_help()
        sys.exit(0)

    if not args.no_update:
        update_ytdlp()

    # ── Build extra yt-dlp args ───────────────────────────────────────────────
    extra_args:  list[str] = list(extra)
    auto_format: bool      = not args.format and not args.audio_only
    if args.format:
        extra_args += ["-f", args.format]

    archive_path: Optional[Path] = Path(args.archive) if args.archive else None
    Path(args.output).mkdir(parents=True, exist_ok=True)

    # ── UI bootstrap ──────────────────────────────────────────────────────────
    console = Console()
    console.print(
        Panel.fit(
            f"[bold cyan]yt-dlp parallel downloader[/bold cyan]\n"
            f"[dim]{len(urls)} URL(s) · {args.workers} download workers · "
            f"output → [underline]{args.output}[/underline]"
            + (f" · archive → [underline]{args.archive}[/underline]" if args.archive else "")
            + "[/dim]",
            border_style="cyan",
        )
    )

    states = [DownloadState(url=u) for u in urls]
    refresh_rate = 8  # Hz

    with Live(console=console, refresh_per_second=refresh_rate) as live:

        # ── Phase 1: parallel archive pre-resolution ──────────────────────────
        if archive_path is not None:
            downloaded = load_archive(archive_path)
            console.log(
                f"[dim]Archive loaded — {len(downloaded)} previously downloaded item(s)[/dim]"
            )
            with ThreadPoolExecutor(max_workers=args.resolve_workers) as resolver:
                res_futures = {
                    resolver.submit(resolve_state, s, downloaded): s
                    for s in states
                }
                while not all(f.done() for f in res_futures):
                    live.update(build_table(states))
                    time.sleep(1 / refresh_rate)
                live.update(build_table(states))
        else:
            downloaded = set()

        skipped  = sum(1 for s in states if s.status == STATUS_SKIPPED)
        to_fetch = [s for s in states if s.status != STATUS_SKIPPED]

        if skipped:
            console.log(f"[dim green]⏭  {skipped} URL(s) skipped (already in archive)[/dim green]")

        if not to_fetch:
            console.log("[bold green]Nothing new to download — all done![/bold green]")
            live.update(build_table(states))
            return

        # ── Phase 2: parallel downloads ───────────────────────────────────────
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(
                    download, s, args.output, extra_args,
                    auto_format, archive_path, args.proxy, args.audio_only
                ): s
                for s in to_fetch
            }

            while not all(f.done() for f in futures):
                live.update(build_table(states))
                time.sleep(1 / refresh_rate)

            live.update(build_table(states))

    # ── Summary ───────────────────────────────────────────────────────────────
    done    = sum(1 for s in states if s.status == STATUS_DONE)
    errors  = sum(1 for s in states if s.status == STATUS_ERROR)
    skipped = sum(1 for s in states if s.status == STATUS_SKIPPED)

    console.print(
        f"\n[bold green]✅ {done} downloaded[/bold green]"
        + (f"  [dim green]⏭  {skipped} skipped[/dim green]" if skipped else "")
        + (f"  [bold red]❌ {errors} failed[/bold red]"    if errors  else "")
    )
    if errors:
        for s in states:
            if s.status == STATUS_ERROR:
                console.print(f"  [red]• {s.url}[/red] — {s.error}")


if __name__ == "__main__":
    main()