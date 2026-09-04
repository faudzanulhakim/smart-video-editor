"""
Proof-of-concept: automatic dubbing (translate speech + generate new-language
audio + mux it onto the original video).

Pipeline:
  1. translate_segments()       -- translate each transcript segment's text to
     the target language, using the same OpenAI-compatible AI provider
     already configured for the other AI features (AI_API_KEY / AI_BASE_URL /
     AI_MODEL from .env).
  2. detect_segment_genders()   -- (optional) estimate male/female per
     segment from the pitch of the ORIGINAL audio, so the TTS voice used
     can roughly match the original speaker's gender.
  3. synthesize_segment_audio() -- generate a TTS audio clip per segment
     using edge-tts (free, no API key needed, includes native Indonesian
     voices), picking a male or female voice per segment based on
     detect_segment_genders() when provided.
  4. build_dubbed_track()       -- time-stretch each clip (via ffmpeg's
     atempo filter) to fit its original segment's time slot, then stitch
     all clips together (with silence filling the gaps) into one
     continuous audio track matching the video's timeline.
  5. mux_dubbed_audio()         -- replace the video's original audio track
     with the new dubbed track.

`dub_video()` runs all steps end-to-end.

Requires: `pip install edge-tts librosa numpy` (see requirements.txt).

Known limitations (proof-of-concept, not production-ready):
  - No lip-sync -- mouth movement in the video still matches the original
    language.
  - Very short segments may need heavy speed-up to fit their original time
    slot; extreme cases are clamped (see MIN/MAX_ATEMPO) rather than
    distorted further, so a segment can end up drifting slightly past its
    original slot instead of sounding unnatural.
  - Gender detection is a rough pitch-based heuristic, not real speaker
    identification -- it can misclassify on noisy audio, background music,
    singing, or children's voices, and only distinguishes two voices (no
    true multi-speaker/voice-cloning support).
  - Background music/sound effects in the original audio are lost -- the
    original audio track is fully replaced, not mixed with the dub.
"""

import asyncio
import re
import subprocess
from pathlib import Path

import numpy as np

from core.ai_helper import _get_config, _client, LANGUAGE_NAMES

# Default edge-tts voice per target language (free, no API key required),
# used when gender detection is off or inconclusive.
# Run `edge-tts --list-voices` for the full list of available voices.
DEFAULT_VOICES = {
    "id": "id-ID-GadisNeural",   # Indonesian, female
    "en": "en-US-JennyNeural",   # English, female
}

# Male/female edge-tts voice per target language, used when a segment's
# gender was detected via detect_segment_genders().
MALE_VOICES = {
    "id": "id-ID-ArdiNeural",
    "en": "en-US-GuyNeural",
}
FEMALE_VOICES = {
    "id": "id-ID-GadisNeural",
    "en": "en-US-JennyNeural",
}

# Rough male/female boundary for adult speech fundamental frequency (F0).
# Typical adult male speech: ~85-180 Hz. Typical adult female speech:
# ~165-255 Hz. 165 Hz sits in the overlap zone as a reasonable cutoff.
PITCH_THRESHOLD_HZ = 165.0

MIN_ATEMPO = 0.85   # don't slow a clip down more than this (starts sounding unnatural)
MAX_ATEMPO = 1.6    # don't speed a clip up more than this (starts sounding unnatural)


# ----------------------------------------------------------------------
# 0. (Optional) Estimate speaker gender per segment from the original audio
# ----------------------------------------------------------------------
def _extract_audio_array(video_path, sr=16000):
    """Extract the full audio track of a video as a mono float32 numpy
    array via ffmpeg, without needing to decode/read the video container
    with a separate audio library."""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-",
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    audio = np.frombuffer(result.stdout, dtype=np.float32)
    return audio, sr


def detect_segment_genders(video_path, segments, default="female"):
    """
    Estimate the speaker's gender per segment by analyzing the pitch
    (fundamental frequency) of the ORIGINAL audio in that segment's time
    range. Returns a list of "male"/"female" strings, one per segment, in
    the same order as `segments`.

    This is a rough heuristic based on typical adult pitch ranges -- NOT
    real speaker identification. It can misclassify on noisy/music-heavy
    audio, singing, or children's/unusually pitched voices. Segments too
    short or too quiet to get a reliable pitch reading fall back to
    `default`.
    """
    try:
        import librosa
    except ImportError:
        print(
            "  [dubbing] librosa not installed -- skipping gender detection, "
            "using the default voice for all segments. Run: pip install librosa",
            flush=True,
        )
        return [default] * len(segments)

    try:
        audio, sr = _extract_audio_array(video_path)
    except Exception as e:
        print(f"  [dubbing] Could not extract audio for gender detection ({e!r}), "
              f"using the default voice for all segments.", flush=True)
        return [default] * len(segments)

    genders = []
    for seg in segments:
        start_sample = int(seg["start"] * sr)
        end_sample = int(seg["end"] * sr)
        clip = audio[start_sample:end_sample]

        if len(clip) < sr * 0.2:  # too short (<0.2s) to get a reliable pitch reading
            genders.append(default)
            continue

        try:
            f0, _voiced_flag, _voiced_prob = librosa.pyin(
                clip,
                fmin=float(librosa.note_to_hz("C2")),   # ~65 Hz
                fmax=float(librosa.note_to_hz("C6")),   # ~1047 Hz
                sr=sr,
            )
            voiced_f0 = f0[~np.isnan(f0)]
            if len(voiced_f0) == 0:
                genders.append(default)
                continue
            median_f0 = float(np.median(voiced_f0))
            genders.append("male" if median_f0 < PITCH_THRESHOLD_HZ else "female")
        except Exception:
            genders.append(default)

    return genders


# ----------------------------------------------------------------------
# 1. Translate transcript segments to the target language
# ----------------------------------------------------------------------
def translate_segments(segments, target_language="id"):
    """
    Translate each segment's text to `target_language` ("id" or "en") using
    the configured AI provider. Timestamps are left unchanged.

    Returns a new list of segments: [{start, end, text}, ...] with `text`
    replaced by the translation. Falls back to the original text for any
    line the AI response doesn't account for.
    """
    if not segments:
        return []

    api_key, base_url, model = _get_config()
    client = _client(base_url, api_key)
    lang_name = LANGUAGE_NAMES.get(target_language, target_language)

    # Translate all segments in a single request (as a numbered list) so the
    # AI has full context and it's one API call instead of one per segment.
    numbered = "\n".join(f"{i + 1}. {seg['text']}" for i, seg in enumerate(segments))
    prompt = f"""Translate each numbered line below into {lang_name}. Keep the
same numbering, one translated line per input line, and do not merge, skip,
or reorder any line -- there are {len(segments)} lines total.

{numbered}

Reply ONLY with the numbered translated lines, no other text."""

    resp = client.chat.completions.create(
        model=model,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = (resp.choices[0].message.content or "").strip()

    translated = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.*)", line)
        if m:
            translated[int(m.group(1))] = m.group(2).strip()

    out = []
    for i, seg in enumerate(segments):
        new_text = translated.get(i + 1, seg["text"])  # fall back to original if a line is missing
        out.append({"start": seg["start"], "end": seg["end"], "text": new_text})
    return out


# ----------------------------------------------------------------------
# 2. Text-to-speech per segment (edge-tts)
# ----------------------------------------------------------------------
async def _edge_tts_save(text, voice, out_path):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out_path))


def synthesize_segment_audio(segments, tmp_dir, target_language="id", voice=None, genders=None):
    """
    Generate one TTS audio file per segment, saved into `tmp_dir`.
    Returns a list of Path objects (or None for empty-text segments), same
    order/length as `segments`.

    Voice selection priority per segment:
      1. If `genders` is given (list of "male"/"female", same length as
         `segments`, e.g. from detect_segment_genders()), use the
         matching MALE_VOICES/FEMALE_VOICES entry for `target_language`.
      2. Else, use `voice` if explicitly given.
      3. Else, fall back to DEFAULT_VOICES[target_language].
    """
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fallback_voice = voice or DEFAULT_VOICES.get(target_language, DEFAULT_VOICES["id"])

    paths = []
    for i, seg in enumerate(segments):
        text = seg["text"].strip()
        if not text:
            paths.append(None)
            continue

        if genders and i < len(genders):
            voice_map = MALE_VOICES if genders[i] == "male" else FEMALE_VOICES
            seg_voice = voice_map.get(target_language, fallback_voice)
        else:
            seg_voice = fallback_voice

        out_path = tmp_dir / f"seg_{i:04d}.mp3"
        asyncio.run(_edge_tts_save(text, seg_voice, out_path))
        paths.append(out_path)
    return paths


# ----------------------------------------------------------------------
# 3. Time-stretch each clip to fit its slot, stitch into one track
# ----------------------------------------------------------------------
def _probe_duration(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        return float(result.stdout.strip())
    except (ValueError, TypeError):
        return None


def _atempo_filter_chain(factor):
    """ffmpeg's atempo filter only accepts 0.5-2.0 per instance; chain
    multiple instances for factors outside that range. Not needed given our
    MIN/MAX_ATEMPO clamp (0.85-1.6), but kept here for safety."""
    filters = []
    remaining = factor
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.4f}")
    return ",".join(filters)


def _make_silence(duration, out_path, sample_rate=24000):
    if duration <= 0:
        return None
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono",
        "-t", f"{duration:.3f}",
        "-q:a", "9",
        str(out_path),
    ]
    subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return out_path


def build_dubbed_track(segments, audio_paths, total_duration, tmp_dir, output_path):
    """
    Stitch per-segment TTS clips into one continuous audio track spanning
    `total_duration` seconds. Each clip is time-stretched (ffmpeg atempo) to
    fit its segment's [start, end] slot; silence fills the gaps in between.
    """
    tmp_dir = Path(tmp_dir)
    parts = []
    cursor = 0.0

    for i, (seg, clip_path) in enumerate(zip(segments, audio_paths)):
        gap = seg["start"] - cursor
        if gap > 0.02:
            silence_path = tmp_dir / f"gap_{i:04d}.mp3"
            _make_silence(gap, silence_path)
            parts.append(silence_path)
            cursor += gap

        if clip_path is not None:
            target_duration = max(0.1, seg["end"] - seg["start"])
            clip_duration = _probe_duration(clip_path) or target_duration
            factor = clip_duration / target_duration
            factor = max(MIN_ATEMPO, min(MAX_ATEMPO, factor))

            adjusted_path = tmp_dir / f"seg_{i:04d}_adj.mp3"
            cmd = [
                "ffmpeg", "-y", "-i", str(clip_path),
                "-filter:a", _atempo_filter_chain(factor),
                str(adjusted_path),
            ]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            parts.append(adjusted_path)
            cursor += _probe_duration(adjusted_path) or (clip_duration / factor)
        else:
            cursor = max(cursor, seg["end"])

    if cursor < total_duration:
        trailing_path = tmp_dir / "trailing_silence.mp3"
        _make_silence(total_duration - cursor, trailing_path)
        parts.append(trailing_path)

    if not parts:
        raise RuntimeError("No audio segments to build the dubbed track from.")

    concat_list_path = tmp_dir / "concat_list.txt"
    with open(concat_list_path, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{Path(p).resolve()}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list_path),
        "-c:a", "libmp3lame", "-b:a", "192k",
        str(output_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not Path(output_path).exists():
        raise RuntimeError(
            f"Failed to build the dubbed audio track. ffmpeg details:\n{result.stdout[-1500:]}"
        )
    return output_path


# ----------------------------------------------------------------------
# 4. Mux the dubbed audio onto the original video (replace the audio track)
# ----------------------------------------------------------------------
def mux_dubbed_audio(video_path, dubbed_audio_path, output_path):
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(dubbed_audio_path),
        "-map", "0:v",
        "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        str(output_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if not Path(output_path).exists():
        raise RuntimeError(
            f"Failed to mux the dubbed audio onto the video. ffmpeg details:\n{result.stdout[-1500:]}"
        )
    return output_path


# ----------------------------------------------------------------------
# End-to-end helper
# ----------------------------------------------------------------------
def dub_video(video_path, segments, output_path, tmp_dir, target_language="id", voice=None,
              detect_gender=True):
    """
    Full pipeline: translate segments -> (optionally) detect per-segment
    gender -> TTS per segment -> build the dubbed track -> mux it onto the
    video.

    `segments` should come from core.editor.transcribe() run on the
    ORIGINAL video with task="transcribe" (i.e. text in the video's
    original language, NOT already translated by Whisper).

    `detect_gender=True` (default) picks a male or female TTS voice per
    segment based on detect_segment_genders(); set to False (or pass an
    explicit `voice`) to use a single voice for the whole video instead.
    """
    from core.editor import _probe_duration as _video_duration

    video_path = Path(video_path)
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    print(f"  [dubbing] Translating {len(segments)} segment(s) to '{target_language}'...", flush=True)
    translated = translate_segments(segments, target_language)

    genders = None
    if detect_gender and voice is None:
        print("  [dubbing] Estimating speaker gender per segment...", flush=True)
        genders = detect_segment_genders(video_path, segments)

    print("  [dubbing] Generating TTS audio per segment...", flush=True)
    audio_paths = synthesize_segment_audio(
        translated, tmp_dir / "tts", target_language, voice, genders=genders
    )

    total_duration = _video_duration(video_path) or (segments[-1]["end"] if segments else 0)

    print("  [dubbing] Building the dubbed audio track...", flush=True)
    dubbed_track_path = tmp_dir / "dubbed_track.mp3"
    build_dubbed_track(translated, audio_paths, total_duration, tmp_dir, dubbed_track_path)

    print("  [dubbing] Muxing the dubbed audio onto the video...", flush=True)
    mux_dubbed_audio(video_path, dubbed_track_path, output_path)

    return output_path


# ----------------------------------------------------------------------
# Quick manual test (not the CLI / web app -- just for trying this module
# out on its own):
#
#   python -m core.dubbing my_english_video.mp4 dubbed_output.mp4
#
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import tempfile
    from core.editor import ensure_playable, transcribe

    if len(sys.argv) < 3:
        print("Usage: python -m core.dubbing <input_video> <output_video> [target_lang]")
        sys.exit(1)

    in_path = ensure_playable(sys.argv[1])
    out_path = sys.argv[2]
    lang = sys.argv[3] if len(sys.argv) > 3 else "id"

    print("Transcribing original audio...")
    segs = transcribe(in_path, language=None, task="transcribe")

    with tempfile.TemporaryDirectory() as td:
        dub_video(in_path, segs, out_path, td, target_language=lang)

    print(f"Done -> {out_path}")
