#!/usr/bin/env python3
"""Transcribe audio with MLX Whisper (Apple Silicon) or faster-whisper.

Writes UTF-8 SRT and emits one JSON object per line to stdout for progress.
Diagnostics from dependencies stay on stderr so stdout remains machine-readable.
"""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import threading
from pathlib import Path
from typing import Any

MLX_TURBO_REPO = "mlx-community/whisper-large-v3-turbo"
MLX_MODEL_ALIASES = {
    "large-v3-turbo": MLX_TURBO_REPO,
    "turbo": MLX_TURBO_REPO,
    "whisper-large-v3-turbo": MLX_TURBO_REPO,
}


def timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def audio_duration(path: Path) -> float | None:
    """Return duration through ffprobe when available, otherwise None."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        duration = float(result.stdout.strip())
        return duration if duration > 0 else None
    except (FileNotFoundError, ValueError, subprocess.CalledProcessError):
        return None


def is_apple_silicon_mac() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def resolve_backend(requested: str) -> str:
    if requested != "auto":
        return requested
    return "mlx" if is_apple_silicon_mac() else "faster-whisper"


def resolve_mlx_model(model: str) -> str:
    if model.startswith("mlx-community/") or model.startswith("/") or Path(model).exists():
        return model
    return MLX_MODEL_ALIASES.get(model, model)


class Progress:
    def __init__(self, duration: float | None, heartbeat_seconds: float) -> None:
        self.duration = duration
        self.heartbeat_seconds = heartbeat_seconds
        self.stage = "loading_model"
        self.processed = 0.0
        self.last_percent = -1
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def emit(self, event: str, **extra: Any) -> None:
        with self.lock:
            payload: dict[str, Any] = {
                "event": event,
                "stage": self.stage,
                "processed_seconds": round(self.processed, 2),
            }
            if self.duration is not None:
                payload["duration_seconds"] = round(self.duration, 2)
                payload["percent"] = min(100, round(self.processed / self.duration * 100, 1))
            payload.update(extra)
            print(json.dumps(payload, ensure_ascii=False), flush=True)

    def heartbeat(self) -> None:
        while not self.stop.wait(self.heartbeat_seconds):
            self.emit("heartbeat")

    def update(self, processed: float) -> None:
        with self.lock:
            self.processed = max(self.processed, processed)
            percent = int(self.processed / self.duration * 100) if self.duration else None
            should_emit = percent is None or percent >= self.last_percent + 1
            if percent is not None and should_emit:
                self.last_percent = percent
        if should_emit:
            self.emit("progress")


def write_srt(path: Path, segments: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for segment in segments:
            text = str(segment.get("text", "")).strip()
            if not text:
                continue
            start = float(segment["start"])
            end = float(segment["end"])
            count += 1
            handle.write(f"{count}\n{timestamp(start)} --> {timestamp(end)}\n{text}\n\n")
            handle.flush()
    return count


def transcribe_faster_whisper(
    args: argparse.Namespace, progress: Progress, duration: float | None
) -> tuple[str, float | None, int]:
    from faster_whisper import WhisperModel

    model = WhisperModel(
        args.model, device=args.device, compute_type=args.compute_type
    )
    progress.stage = "transcribing"
    progress.emit("model_ready", backend="faster-whisper")
    segments, info = model.transcribe(
        str(args.audio), language=args.language, vad_filter=True
    )
    args.srt.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with args.srt.open("w", encoding="utf-8") as handle:
        for count, segment in enumerate(segments, start=1):
            text = segment.text.strip()
            if not text:
                continue
            handle.write(
                f"{count}\n{timestamp(segment.start)} --> {timestamp(segment.end)}\n{text}\n\n"
            )
            handle.flush()
            progress.update(segment.end)
    progress.processed = duration or progress.processed
    return info.language, float(info.language_probability), count


def transcribe_mlx(
    args: argparse.Namespace, progress: Progress, duration: float | None
) -> tuple[str, float | None, int]:
    import tqdm as tqdm_module
    import mlx_whisper

    model_id = resolve_mlx_model(args.model)
    progress.emit("loading_model", backend="mlx", model=model_id)

    class ProgressTqdm(tqdm_module.tqdm):
        def update(self, n: float | int = 1):  # type: ignore[override]
            result = super().update(n)
            if self.total and duration is not None and self.total > 0:
                # content_frames progress ≈ audio progress
                progress.update(duration * min(1.0, float(self.n) / float(self.total)))
            return result

    original_tqdm = tqdm_module.tqdm
    tqdm_module.tqdm = ProgressTqdm  # type: ignore[assignment]
    try:
        progress.stage = "transcribing"
        progress.emit("model_ready", backend="mlx", model=model_id)
        decode_options: dict[str, Any] = {}
        if args.language:
            decode_options["language"] = args.language
        result = mlx_whisper.transcribe(
            str(args.audio),
            path_or_hf_repo=model_id,
            verbose=False,
            **decode_options,
        )
    finally:
        tqdm_module.tqdm = original_tqdm  # type: ignore[assignment]

    segments = list(result.get("segments") or [])
    count = write_srt(args.srt, segments)
    if segments:
        progress.update(float(segments[-1].get("end") or duration or 0))
    progress.processed = duration or progress.processed
    language = str(result.get("language") or args.language or "unknown")
    return language, None, count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--srt", type=Path, required=True)
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--language")
    parser.add_argument(
        "--backend",
        choices=("auto", "mlx", "faster-whisper"),
        default="auto",
        help="auto: MLX Whisper turbo on Apple Silicon Mac; else faster-whisper",
    )
    parser.add_argument("--device", default="auto")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--heartbeat-seconds", type=float, default=2)
    args = parser.parse_args()

    if args.heartbeat_seconds <= 0:
        parser.error("--heartbeat-seconds must be positive")

    if not args.audio.is_file() or args.audio.stat().st_size == 0:
        raise SystemExit(f"audio missing or empty: {args.audio}")

    backend = resolve_backend(args.backend)
    if backend == "mlx" and not is_apple_silicon_mac():
        raise SystemExit("mlx backend requires macOS Apple Silicon (Darwin arm64)")

    duration = audio_duration(args.audio)
    progress = Progress(duration, args.heartbeat_seconds)
    progress.emit(
        "started",
        model=args.model,
        audio=str(args.audio),
        backend=backend,
        platform=f"{platform.system()}-{platform.machine()}",
    )
    heartbeat = threading.Thread(target=progress.heartbeat, daemon=True)
    heartbeat.start()

    try:
        if backend == "mlx":
            language, language_probability, count = transcribe_mlx(
                args, progress, duration
            )
        else:
            language, language_probability, count = transcribe_faster_whisper(
                args, progress, duration
            )
        progress.stage = "complete"
        complete: dict[str, Any] = {
            "language": language,
            "segments": count,
            "srt": str(args.srt),
            "backend": backend,
        }
        if language_probability is not None:
            complete["language_probability"] = round(language_probability, 3)
        if backend == "mlx":
            complete["model"] = resolve_mlx_model(args.model)
        progress.emit("complete", **complete)
    except Exception as exc:
        progress.stage = "error"
        progress.emit("error", message=str(exc), backend=backend)
        raise
    finally:
        progress.stop.set()
        heartbeat.join(timeout=1)


if __name__ == "__main__":
    main()
