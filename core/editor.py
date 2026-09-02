"""
Core video editing functions: potong sepi, subtitle, intro/outro, watermark.
Dipakai bersama oleh CLI (auto_video_editor.py) dan web app (web/main.py).
"""

import subprocess
from pathlib import Path

# --- Kompatibilitas Pillow >= 10 dengan MoviePy 1.0.3 --------------------
# MoviePy 1.0.3 masih memanggil PIL.Image.ANTIALIAS, yang sudah DIHAPUS di
# Pillow 10+ (diganti Image.Resampling.LANCZOS / Image.LANCZOS). Tanpa ini,
# resize gambar (mis. watermark) akan error:
#   AttributeError: module 'PIL.Image' has no attribute 'ANTIALIAS'
# Harus dipasang SEBELUM `from moviepy.editor import ...` di bawah.
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
# Perbaikan file yang metadata durasinya rusak/tidak ada
# (umum terjadi pada .webm hasil rekam browser / MediaRecorder API,
#  di mana ffmpeg -i menampilkan "Duration: N/A")
# ----------------------------------------------------------------------
def _probe_duration(video_path):
    """Return durasi (detik) via ffprobe, atau None kalau tidak terbaca."""
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
    Beberapa file .webm (mis. hasil rekam browser) tidak punya metadata
    durasi yang valid ('Duration: N/A' di ffmpeg), sehingga MoviePy gagal
    membukanya. Fungsi ini me-remux file tsb (tanpa re-encode, cepat) agar
    ffmpeg menghitung ulang durasinya, lalu memvalidasi hasilnya.

    Return: Path ke file yang siap dipakai (bisa sama dengan input kalau
    memang sudah OK, atau file baru hasil remux).
    """
    video_path = Path(video_path)

    if _probe_duration(video_path) is not None:
        return video_path  # durasi normal, tidak perlu diapa-apakan

    if not _has_video_stream(video_path):
        size_mb = video_path.stat().st_size / (1024 * 1024)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", str(video_path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        raise ValueError(
            f"File '{video_path.name}' ({size_mb:.1f} MB diterima server) tidak "
            "memiliki video stream yang valid. Ini berarti file sumbernya sendiri "
            "audio-only, ATAU file rusak/terpotong saat proses upload ke server. "
            "Cek file aslinya di komputer kamu (buka di media player, pastikan "
            "videonya memang ada) sebelum upload ulang.\n"
            f"Detail stream terdeteksi ffprobe:\n{probe.stdout or '(tidak ada stream terbaca sama sekali)'}"
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
            f"Gagal memperbaiki metadata durasi untuk '{video_path.name}'. "
            f"Detail ffmpeg:\n{result.stdout[-1500:]}"
        )

    return fixed_path


# ----------------------------------------------------------------------
# Potong bagian sepi/diam otomatis
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
    Potong bagian sepi langsung lewat ffmpeg (satu pass, filter select/aselect),
    JAUH lebih cepat & stabil dibanding subclip()+concatenate_videoclips() MoviePy
    -- terutama untuk file yang seek-index-nya kurang bagus (mis. .webm dari browser).

    Return: Path video hasil (output_path), atau video_path asli kalau tidak ada
    yang perlu dipotong.
    """
    video_path = Path(video_path)
    output_path = Path(output_path)

    if not silences:
        return video_path

    if duration is None:
        duration = _probe_duration(video_path)
    if duration is None:
        # Tidak bisa menentukan durasi -> jangan coba potong, biar aman
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
        return video_path  # tidak ada yang perlu dipotong

    conditions = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in keep_ranges)
    vf = f"select='{conditions}',setpts=N/FRAME_RATE/TB"
    af = f"aselect='{conditions}',asetpts=N/SR/TB"

    print(f"  ({len(keep_ranges)} bagian video dipertahankan setelah potong sepi)")

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
            f"Gagal memotong bagian sepi. Detail ffmpeg:\n{result.stdout[-1500:]}"
        )
    return output_path


# ----------------------------------------------------------------------
# Subtitle otomatis (speech-to-text) pakai faster-whisper
# ----------------------------------------------------------------------
def transcribe(video_path, model_size="medium", language=None, beam_size=5,
                vad_filter=True, task="transcribe"):
    """
    Transkrip audio -> list of segments [{start, end, text}, ...]

    Untuk akurasi lebih baik dibanding sebelumnya:
    - model_size default dinaikkan ke "medium" (lebih akurat dari "small",
      masih wajar dijalankan di CPU). Kalau butuh lebih akurat lagi dan CPU/waktu
      memungkinkan, bisa pakai "large-v3"; kalau butuh lebih cepat, "base"/"small".
    - beam_size=5 -> pencarian kandidat teks lebih luas per segmen (lebih presisi,
      sedikit lebih lambat dari beam_size=1/greedy).
    - vad_filter=True -> pakai voice-activity-detection supaya bagian non-suara
      (musik, noise, silence sisa) tidak ikut coba ditranskrip jadi teks ngawur.
      Kalau proses VAD gagal (bug dikenal di faster-whisper pada audio pendek/
      nyaris tanpa suara -> "tuple index out of range"), otomatis fallback
      dicoba ulang TANPA vad_filter supaya transkripsi tetap jalan.
    - task="transcribe" (default) -> teks output dalam bahasa aslinya (`language`).
      task="translate" -> Whisper langsung MENERJEMAHKAN ke Bahasa Inggris
      (kemampuan bawaan model, terlepas dari bahasa sumbernya) -- tidak butuh
      API AI eksternal sama sekali untuk fitur subtitle Bahasa Inggris.
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
            f"  [transcribe] VAD filter gagal ({e!r}), mencoba ulang tanpa VAD...",
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


# Style subtitle default: bold, putih dengan outline hitam tebal + shadow tipis
# -> tetap kebaca jelas di atas video apapun (terang/gelap), gaya umum dipakai
# di video sosial media (Reels/TikTok/Shorts), lebih "profesional" dibanding
# subtitle putih tipis default ffmpeg.
DEFAULT_SUBTITLE_STYLE = {
    "font_name": "Roboto",       # fallback otomatis ke font lain kalau Roboto tidak ada
    "font_size": 16,
    "primary_color": "&H00FFFFFF",   # putih (format ASS: &HAABBGGRR)
    "outline_color": "&H00000000",   # hitam
    "back_color": "&H64000000",      # hitam transparan (dipakai kalau BorderStyle=3/4)
    "bold": 1,
    "outline_width": 1.6,
    "shadow": 0.6,
    "alignment": 2,   # 2 = tengah bawah (posisi subtitle standar)
    "margin_v": 40,
}


def _probe_resolution(video_path):
    """Return (width, height) video via ffprobe, atau None kalau gagal."""
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
    Burn subtitle ke video dengan styling custom (font, ukuran, warna, outline).
    `style` (opsional) meng-override sebagian/semua key di DEFAULT_SUBTITLE_STYLE.
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

    # PENTING: tanpa "original_size", libass (dipakai filter subtitles ffmpeg)
    # bisa salah menebak resolusi asli video lalu men-scale ukuran font secara
    # tidak proporsional (jadi jauh lebih besar dari FontSize yang diminta).
    # Dengan kasih tahu resolusi video sebenarnya, scaling-nya jadi akurat.
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
        raise RuntimeError(f"Gagal burn subtitle. Detail ffmpeg:\n{result.stdout[-1500:]}")


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
    Tempel watermark langsung lewat ffmpeg (scale2ref + overlay), satu pass,
    JAUH lebih cepat dibanding compositing frame-by-frame lewat MoviePy
    (CompositeVideoClip) -- terutama untuk video yang agak panjang.

    Return: Path video hasil (output_path), atau video_path asli kalau
    watermark tidak valid/tidak di-set.
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
        raise RuntimeError(f"Gagal menambahkan watermark. Detail ffmpeg:\n{result.stdout[-1500:]}")
    return output_path


# ----------------------------------------------------------------------
# Ekstrak audio dari video (convert video -> audio)
# ----------------------------------------------------------------------
def extract_audio(video_path, output_path, audio_format="mp3", bitrate="192k"):
    """
    Ambil track audio dari video, simpan sebagai file audio terpisah.
    audio_format: "mp3", "m4a"/"aac", atau "wav" (lossless, file lebih besar).
    Return: Path file audio hasil.
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
        "-vn",  # buang video, ambil audio saja
        *codec_args,
        str(output_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(
            f"Gagal ekstrak audio (kemungkinan video tidak punya track audio). "
            f"Detail ffmpeg:\n{result.stdout[-1500:]}"
        )
    return output_path


# ----------------------------------------------------------------------
# Potong reel highlight dari segmen waktu tertentu
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
