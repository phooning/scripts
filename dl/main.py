#!/usr/bin/env python3
"""
Parallel yt-dlp downloader with live Rich progress display.

Usage:
    python dl [URL ...]
    python dl --file urls.txt
    python dl --workers 4 URL1 URL2 URL3

Dependencies:
    pip install yt-dlp rich
"""

import argparse
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    TaskID,
)
from rich.table import Table
from rich.text import Text

STATUS_WAITING = "waiting"
STATUS_FETCHING = "fetching"
STATUS_DOWNLOAD = "downloading"
STATUS_MERGE = "merging"
STATUS_DONE = "done"
STATUS_ERROR = "error"

STATUS_STYLE = {
    STATUS_WAITING: ("dim", "⏳"),
    STATUS_FETCHING: ("yellow", "🔍"),
    STATUS_DOWNLOAD: ("cyan", "⬇ "),
    STATUS_MERGE: ("magenta", "🔀"),
    STATUS_DONE: ("bold green", "✅"),
    STATUS_ERROR: ("bold red", "❌"),
}


@dataclass
class DownloadState:
    url: str
    task_id: Optional[TaskID] = None
    status: str = STATUS_WAITING
    title: str = ""
    percent: float = 0.0
    speed: str = ""
    eta: str = ""
    error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
_SPEED_RE = re.compile(r"(\d+(?:\.\d+)?(?:K|M|G)iB/s)")
_ETA_RE = re.compile(r"ETA\s+(\S+)")
_DEST_RE = re.compile(r"\[download\] Destination: (.+)")
_TITLE_RE = re.compile(r"\[(?:youtube|soundcloud|vimeo|info)\].*?: (.+)")
_MERGE_RE = re.compile(r"\[Merger\]|Merging")


def parse_ytdlp_line(line: str, state: DownloadState) -> None:
    """Update state from a single yt-dlp output line."""
    with state.lock:
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


_FORMAT_ID_RE = re.compile(
    r"^(\S+)\s+\|\s+(\S+)\s+\|.*?\|\s+(video only|audio only)?", re.IGNORECASE
)
_FORMAT_ROW_RE = re.compile(r"^(\S+)\s")  # first token = format id


def _last_format_id(lines: list[str], kind: str) -> Optional[str]:
    """Return the last format-id line matching kind ('video only' / 'audio only')."""
    last = None
    for line in lines:
        if kind.lower() in line.lower():
            m = _FORMAT_ROW_RE.match(line.strip())
            if m:
                last = m.group(1)
    return last


def _any_format_id(lines: list[str]) -> Optional[str]:
    """Return the last non-header, non-separator format-id from the table."""
    last = None
    for line in lines:
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("ID")
            or set(stripped).issubset({"-", "+", "|", " "})
        ):
            continue
        m = _FORMAT_ROW_RE.match(stripped)
        if m and m.group(1) not in ("ID", "["):
            last = m.group(1)
    return last


def resolve_format(url: str, state: DownloadState) -> Optional[str]:
    """
    Run `yt-dlp -F <url>` and return a format string.
    If both video-only and audio-only rows exist, returns 'last_video+last_audio'.
    Otherwise returns the last listed format id.
    Returns None on failure (caller falls back to yt-dlp default).
    """
    with state.lock:
        state.status = STATUS_FETCHING
        state.title = "querying formats…"

    try:
        result = subprocess.run(
            ["yt-dlp", "-F", url],
            capture_output=True,
            text=True,
            timeout=60,
        )
        lines = result.stdout.splitlines()

        last_video = _last_format_id(lines, "video only")
        last_audio = _last_format_id(lines, "audio only")

        if last_video and last_audio:
            return f"{last_video}+{last_audio}"

        fallback = _any_format_id(lines)
        return fallback  # may be None if parsing failed

    except Exception:
        return None


def download(
    state: DownloadState, output_dir: str, extra_args: list[str], auto_format: bool
) -> None:
    fmt_args: list[str] = []
    if auto_format:
        fmt = resolve_format(state.url, state)
        if fmt:
            fmt_args = ["-f", fmt]
        # if fmt is None we let yt-dlp pick its own default

    cmd = [
        "yt-dlp",
        "--newline",
        "-o",
        f"{output_dir}/%(title)s.%(ext)s",
        *fmt_args,
        *extra_args,
        state.url,
    ]

    with state.lock:
        state.status = STATUS_FETCHING
        if not state.title or state.title == "querying formats…":
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
            raise RuntimeError("Failed to open stdout pipe for yt-dlp process")

        for line in proc.stdout:
            parse_ytdlp_line(line.rstrip(), state)

        proc.wait()
        with state.lock:
            if proc.returncode == 0:
                state.status = STATUS_DONE
                state.percent = 100.0
            else:
                state.status = STATUS_ERROR
                state.error = f"exit code {proc.returncode}"
    except FileNotFoundError:
        with state.lock:
            state.status = STATUS_ERROR
            state.error = "yt-dlp not found — install with: pip install yt-dlp"
    except Exception as exc:
        with state.lock:
            state.status = STATUS_ERROR
            state.error = str(exc)


def build_table(states: list[DownloadState]) -> Table:
    table = Table(
        box=box.ROUNDED,
        expand=True,
        show_header=True,
        header_style="bold bright_white on grey23",
        border_style="grey42",
    )
    table.add_column("#", style="dim", width=3, no_wrap=True)
    table.add_column("Status", style="", width=12, no_wrap=True)
    table.add_column("Title / URL", ratio=3, no_wrap=True)
    table.add_column("Progress", width=22, no_wrap=True)
    table.add_column("Speed", style="cyan", width=12, no_wrap=True)
    table.add_column("ETA", style="bright_black", width=7, no_wrap=True)

    for i, s in enumerate(states, 1):
        with s.lock:
            style, icon = STATUS_STYLE.get(s.status, ("", "?"))
            label = Text(f"{icon} {s.status}", style=style)

            display = s.title if s.title else s.url
            if len(display) > 55:
                display = display[:52] + "…"

            # ASCII progress bar (22 chars wide)
            filled = int(s.percent / 100 * 20)
            bar = f"[{'█' * filled}{'░' * (20 - filled)}] {s.percent:5.1f}%"
            bar_text = Text(bar, style="cyan" if s.status == STATUS_DOWNLOAD else "dim")

            if s.status == STATUS_ERROR:
                bar_text = Text(s.error[:24], style="red")

            table.add_row(
                str(i),
                label,
                display,
                bar_text,
                s.speed or "—",
                s.eta or "—",
            )

    return table


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parallel yt-dlp downloader with live progress display."
    )
    parser.add_argument("urls", nargs="*", help="Media URLs to download")
    parser.add_argument(
        "--file",
        "-f",
        metavar="FILE",
        help="Text file with one URL per line",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=3,
        metavar="N",
        help="Max parallel downloads (default: 3)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="downloads",
        metavar="DIR",
        help="Output directory (default: ./downloads)",
    )
    parser.add_argument(
        "--format",
        default=None,
        metavar="FMT",
        help="yt-dlp format string (e.g. 'bestvideo+bestaudio/best')",
    )
    parser.add_argument(
        "--audio-only",
        "-a",
        action="store_true",
        help="Extract audio only (passes -x --audio-format mp3)",
    )
    args, extra = parser.parse_known_args()

    # Collect URLs
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

    # Build extra yt-dlp args
    extra_args: list[str] = list(extra)
    auto_format = not args.format and not args.audio_only
    if args.format:
        extra_args += ["-f", args.format]
    if args.audio_only:
        extra_args += ["-x", "--audio-format", "mp3"]

    # Prepare output directory
    Path(args.output).mkdir(parents=True, exist_ok=True)

    console = Console()
    console.print(
        Panel.fit(
            f"[bold cyan]yt-dlp parallel downloader[/bold cyan]\n"
            f"[dim]{len(urls)} URL(s) · {args.workers} workers · output → [underline]{args.output}[/underline][/dim]",
            border_style="cyan",
        )
    )

    states = [DownloadState(url=u) for u in urls]
    refresh_per_second = 8

    with Live(console=console, refresh_per_second=refresh_per_second) as live:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(download, s, args.output, extra_args, auto_format): s
                for s in states
            }

            while not all(f.done() for f in futures):
                live.update(build_table(states))
                threading.Event().wait(1 / refresh_per_second)

            # Final render
            live.update(build_table(states))

    # Summary
    done = sum(1 for s in states if s.status == STATUS_DONE)
    error = sum(1 for s in states if s.status == STATUS_ERROR)
    console.print(
        f"\n[bold green]✅ {done} succeeded[/bold green]"
        + (f"  [bold red]❌ {error} failed[/bold red]" if error else "")
    )
    if error:
        for s in states:
            if s.status == STATUS_ERROR:
                console.print(f"  [red]• {s.url}[/red] — {s.error}")


if __name__ == "__main__":
    main()
