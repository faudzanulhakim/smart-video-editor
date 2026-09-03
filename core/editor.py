"""
Core video editing functions: silence removal, subtitles, intro/outro, watermark.
Shared by the CLI (auto_video_editor.py) and the web app (web/main.py).
"""

import subprocess
from pathlib import Path

# --- Pillow >= 10 compatibility with MoviePy 1.0.3 ------------------------
# MoviePy 1.0.3 still calls PIL.Image.ANTIALIAS, which has been REMOVED in
# Pillow 10+ (replaced by Image.Resampling.LANCZOS / Image.LANCZOS). Without
# this, resizing images (e.g. watermark) will raise:
#   AttributeError: module 'PIL.Image' has no attribute 'ANTIALIAS'
# Must be set BEFORE `from moviepy.editor import ...` below.
import PIL.Image
if not hasattr(PIL.Image, "ANTIALIAS"):
    PIL.Image.ANTIALIAS = PIL.Image.LANCZOS
# ---------------------------------------------------------------------------

from moviepy.editor import (
    VideoFileClip,
    concatenate_videoclips,
    ImageClip,
    CompositeVideoClip,
)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


# ----------------------------------------------------------------------
# Fix files with broken/missing duration metadata
# (common with .webm files recorded via a browser / MediaRecorder API,
#  where ffmpeg -i shows "Duration: N/A")
# ----------------------------------------------------------------------
def _probe_duration(video_path):
    """Return duration (seconds) via ffprobe, or None if it can't be read."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        return None


def _has_video_stream(video_path):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0",
        str(video_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return "video" in result.stdout


def ensure_playable(video_path):
    """
    Some .webm files (e.g. recorded in a browser) don't have valid duration
    metadata ('Duration: N/A' in ffmpeg), which causes MoviePy to fail when
    opening them. This function remuxes the file (no re-encode, fast) so
    ffmpeg recalculates its duration, then validates the result.

    Return: Path to a file that's ready to use (may be the same as the
    input if it was already fine, or a new remuxed file).
    """
    video_path = Path(video_path)

    if _probe_duration(video_path) is not None:
        return video_path  # duration is normal, nothing to do

    if not _has_video_stream(video_path):
        size_mb = video_path.stat().st_size / (1024 * 1024)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", str(video_path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        raise ValueError(
            f"File '{video_path.name}' ({size_mb:.1f} MB received by the server) does not "
            "have a valid video stream. This means either the source file itself is "
            "audio-only, OR the file got corrupted/truncated while uploading to the "
            "server. Check the original file on your computer (open it in a media "
            "player and make sure the video is actually there) before re-uploading.\n"
            f"ffprobe detected stream details:\n{probe.stdout or '(no stream detected at all)'}"
        )

    fixed_path = video_path.with_name(f"{video_path.stem}__fixed.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-fflags", "+genpts",
        "-i", str(video_path),
        "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "aac",
        str(fixed_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    if not fixed_path.exists() or _probe_duration(fixed_path) is None:
        raise ValueError(
            f"Failed to fix the duration metadata for '{video_path.name}'. "
            f"ffmpeg details:\n{result.stdout[-1500:]}"
        )

    return fixed_path


# ----------------------------------------------------------------------
# Automatic silence removal
# ----------------------------------------------------------------------
def detect_silence_ranges(video_path, silence_thresh_db=-35, min_silence_len=0.6):
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-af", f"silencedetect=noise={silence_thresh_db}dB:d={min_silence_len}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    log = result.stdout

    silences = []
    start = None
    for line in log.splitlines():
        if "silence_start" in line:
            try:
                start = float(line.split("silence_start:")[1].strip())
            except (IndexError, ValueError):
                start = None
        elif "silence_end" in line and start is not None:
            try:
                end_part = line.split("silence_end:")[1].strip()
                end = float(end_part.split("|")[0].strip())
                silences.append((start, end))
            except (IndexError, ValueError):
                pass
            start = None
    return silences


def remove_silence(video_path, silences, output_path, padding=0.15, duration=None):
    """
    Remove silent segments directly via ffmpeg (single pass, select/aselect
    filter), MUCH faster and more stable than subclip()+concatenate_videoclips()
    in MoviePy -- especially for files with a poor seek index (e.g. .webm
    recorded in a browser).

    Return: Path to the resulting video (output_path), or the original
    video_path if nothing needed to be cut.
    """
    video_path = Path(video_path)
    output_path = Path(output_path)

    if not silences:
        return video_path

    if duration is None:
        duration = _probe_duration(video_path)
    if duration is None:
        # Can't determine duration -> don't attempt to cut, to stay safe
        return video_path

    keep_ranges = []
    cursor = 0.0
    for s, e in silences:
        s = max(0.0, s + padding)
        e = min(duration, e - padding)
        if s > cursor:
            keep_ranges.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        keep_ranges.append((cursor, duration))

    keep_ranges = [(s, e) for s, e in keep_ranges if e > s]
    if not keep_ranges:
        return video_path
    if len(keep_ranges) == 1 and keep_ranges[0] == (0.0, duration):
        return video_path  # nothing needs to be cut

    conditions = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keep_ranges)
    vf = f"select='{conditions}',setpts=N/FRAME_RATE/TB"
    af = f"aselect='{conditions}',asetpts=N/SR/TB"

    print(f"  ({len(keep_ranges)} video segment(s) kept after silence removal)")

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vf", vf,
        "-af", af,
        "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "aac",
        str(output_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(
            f"Failed to remove silent segments. ffmpeg details:\n{result.stdout[-1500:]}"
        )
    return output_path


# ----------------------------------------------------------------------
# Automatic subtitles (speech-to-text) using faster-whisper
# ----------------------------------------------------------------------
def transcribe(video_path, model_size="medium", language=None, beam_size=5,
                vad_filter=True, task="transcribe"):
    """
    Transcribe audio -> list of segments [{start, end, text}, ...]

    For better accuracy compared to before:
    - model_size default raised to "medium" (more accurate than "small",
      still reasonable to run on CPU). If you need even more accuracy and
      have the CPU/time to spare, use "large-v3"; if you need it faster,
      use "base"/"small".
    - beam_size=5 -> a wider search of text candidates per segment (more
      precise, slightly slower than beam_size=1/greedy).
    - vad_filter=True -> uses voice-activity-detection so non-speech parts
      (music, noise, leftover silence) don't get transcribed into garbled
      text. If the VAD process fails (a known bug in faster-whisper on
      short/near-silent audio -> "tuple index out of range"), it
      automatically falls back and retries WITHOUT vad_filter so
      transcription still completes.
    - task="transcribe" (default) -> output text stays in its original
      language (`language`). task="translate" -> Whisper directly
      TRANSLATES to English (a built-in model capability, regardless of
      the source language) -- no external AI API needed at all for the
      English subtitle feature.
    """
    from faster_whisper import WhisperModel

    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    def _run(use_vad):
        raw_segments, _ = model.transcribe(
            str(video_path),
            language=language,
            task=task,
            beam_size=beam_size,
            vad_filter=use_vad,
            vad_parameters={"min_silence_duration_ms": 500} if use_vad else None,
            condition_on_previous_text=True,
        )
        return [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in raw_segments]

    if not vad_filter:
        return _run(False)

    try:
        return _run(True)
    except Exception as e:
        print(
            f"  [transcribe] VAD filter failed ({e!r}), retrying without VAD...",
            flush=True,
        )
        return _run(False)


def write_srt(segments, srt_path):
    def fmt_time(t):
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            f.write(f"{i}\n")
            f.write(f"{fmt_time(seg['start'])} --> {fmt_time(seg['end'])}\n")
            f.write(f"{seg['text']}\n\n")
    return srt_path


# Default subtitle style: bold, white with a thick black outline + subtle
# shadow -> stays clearly readable over any video (bright/dark), a common
# style used in social media videos (Reels/TikTok/Shorts), more
# "professional" than ffmpeg's default thin white subtitle.
DEFAULT_SUBTITLE_STYLE = {
    "font_name": "Roboto",       # falls back automatically to another font if Roboto isn't available
    "font_size": 16,
    "primary_color": "&H00FFFFFF",   # white (ASS format: &HAABBGGRR)
    "outline_color": "&H00000000",   # black
    "back_color": "&H64000000",      # transparent black (used when BorderStyle=3/4)
    "bold": 1,
    "outline_width": 1.6,
    "shadow": 0.6,
    "alignment": 2,   # 2 = bottom center (standard subtitle position)
    "margin_v": 40,
}


def _probe_resolution(video_path):
    """Return (width, height) of the video via ffprobe, or None on failure."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        str(video_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        w, h = result.stdout.strip().split("x")
        return int(w), int(h)
    except (ValueError, AttributeError):
        return None


def burn_subtitle(video_path, srt_path, output_path, style=None):
    """
    Burn subtitles into the video with custom styling (font, size, color,
    outline). `style` (optional) overrides some/all keys in
    DEFAULT_SUBTITLE_STYLE.
    """
    s = dict(DEFAULT_SUBTITLE_STYLE)
    if style:
        s.update(style)

    force_style = (
        f"FontName={s['font_name']},"
        f"FontSize={s['font_size']},"
        f"PrimaryColour={s['primary_color']},"
        f"OutlineColour={s['outline_color']},"
        f"BackColour={s['back_color']},"
        f"Bold={s['bold']},"
        f"Outline={s['outline_width']},"
        f"Shadow={s['shadow']},"
        f"Alignment={s['alignment']},"
        f"MarginV={s['margin_v']}"
    )

    # IMPORTANT: without "original_size", libass (used by ffmpeg's subtitles
    # filter) can guess the video's original resolution wrong and scale the
    # font size disproportionately (ending up much larger than the requested
    # FontSize). Providing the actual video resolution makes the scaling
    # accurate.
    resolution = _probe_resolution(video_path)
    srt_escaped = str(srt_path).replace("\\", "/").replace(":", "\\:")
    if resolution:
        w, h = resolution
        vf = f"subtitles='{srt_escaped}':original_size={w}x{h}:force_style='{force_style}'"
    else:
        vf = f"subtitles='{srt_escaped}':force_style='{force_style}'"

    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vf", vf,
        "-c:a", "copy",
        str(output_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not Path(output_path).exists():
        raise RuntimeError(f"Failed to burn subtitles. ffmpeg details:\n{result.stdout[-1500:]}")


# ----------------------------------------------------------------------
# Intro / Outro / Watermark
# ----------------------------------------------------------------------
def add_intro_outro(clip, intro_path=None, outro_path=None):
    parts = []
    if intro_path and Path(intro_path).exists():
        parts.append(VideoFileClip(intro_path).resize(clip.size))
    parts.append(clip)
    if outro_path and Path(outro_path).exists():
        parts.append(VideoFileClip(outro_path).resize(clip.size))
    return concatenate_videoclips(parts) if len(parts) > 1 else clip


def add_watermark(video_path, watermark_path, output_path, position="bottom-right",
                   opacity=0.7, width_ratio=0.15, margin=20):
    """
    Overlay a watermark directly via ffmpeg (scale2ref + overlay), a single
    pass, MUCH faster than frame-by-frame compositing with MoviePy
    (CompositeVideoClip) -- especially for longer videos.

    Return: Path to the resulting video (output_path), or the original
    video_path if the watermark is invalid/not set.
    """
    video_path = Path(video_path)
    output_path = Path(output_path)

    if not watermark_path or not Path(watermark_path).exists():
        return video_path

    anchors = {
        "bottom-right": (f"main_w-w-{margin}", f"main_h-h-{margin}"),
        "bottom-left": (f"{margin}", f"main_h-h-{margin}"),
        "top-right": (f"main_w-w-{margin}", f"{margin}"),
        "top-left": (f"{margin}", f"{margin}"),
    }
    x_expr, y_expr = anchors.get(position, anchors["bottom-right"])

    filter_complex = (
        f"[1:v][0:v]scale2ref=w=main_w*{width_ratio}:h=ow/mdar[wm][base];"
        f"[wm]format=rgba,colorchannelmixer=aa={opacity}[wm2];"
        f"[base][wm2]overlay=x={x_expr}:y={y_expr}[out]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(watermark_path),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast",
        "-c:a", "copy",
        str(output_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"Failed to add watermark. ffmpeg details:\n{result.stdout[-1500:]}")
    return output_path


# ----------------------------------------------------------------------
# Extract audio from video (convert video -> audio)
# ----------------------------------------------------------------------
def extract_audio(video_path, output_path, audio_format="mp3", bitrate="192k"):
    """
    Extract the audio track from a video and save it as a separate audio file.
    audio_format: "mp3", "m4a"/"aac", or "wav" (lossless, larger file).
    Return: Path to the resulting audio file.
    """
    video_path = Path(video_path)
    output_path = Path(output_path)

    codec_map = {
        "mp3": ["-c:a", "libmp3lame", "-b:a", bitrate],
        "m4a": ["-c:a", "aac", "-b:a", bitrate],
        "aac": ["-c:a", "aac", "-b:a", bitrate],
        "wav": ["-c:a", "pcm_s16le"],
    }
    codec_args = codec_map.get(audio_format, codec_map["mp3"])

    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vn",  # drop video, keep audio only
        *codec_args,
        str(output_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(
            f"Failed to extract audio (the video may not have an audio track). "
            f"ffmpeg details:\n{result.stdout[-1500:]}"
        )
    return output_path


# ----------------------------------------------------------------------
# Cut a highlight reel from specific time segments
# ----------------------------------------------------------------------
def cut_highlights(video_path, ranges, output_path, padding=0.3):
    clip = VideoFileClip(str(video_path))
    duration = clip.duration
    subclips = []
    for r in ranges:
        s = max(0.0, r["start"] - padding)
        e = min(duration, r["end"] + padding)
        if e > s:
            subclips.append(clip.subclip(s, e))
    if not subclips:
        clip.close()
        return None
    final = concatenate_videoclips(subclips)
    final.write_videofile(str(output_path), codec="libx264", audio_codec="aac", logger=None)
    final.close()
    clip.close()
    return output_path
