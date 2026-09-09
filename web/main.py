"""
Auto Video Editor web app.
Run with: uvicorn web.main:app --host 0.0.0.0 --port 8000
"""

import os
import shutil
import sys
import threading
import traceback
import uuid
import hashlib
import json
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.append(str(Path(__file__).resolve().parent.parent))
from core import editor, ai_helper, dubbing
from core.security import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    has_extension,
    is_safe_output_name,
    is_valid_job_id,
    safe_filename,
)

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Auto Video Editor")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# Job status is stored in-memory (sufficient for single-instance / MVP)
JOBS = {}
JOBS_LOCK = threading.Lock()
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "2048")) * 1024 * 1024


def _copy_upload(upload, destination):
    """Copy an upload while enforcing the configured maximum size."""
    total = 0
    with open(destination, "wb") as output:
        while chunk := upload.file.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                output.close()
                Path(destination).unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="Uploaded file is too large")
            output.write(chunk)


@app.get("/")
def index():
    return FileResponse(str(Path(__file__).parent / "static" / "index.html"))


@app.post("/upload")
def upload(
    file: UploadFile = File(...),
    remove_silence: bool = Form(False),
    subtitle: bool = Form(False),
    subtitle_lang: str = Form("id"),
    ai_summary: bool = Form(False),
    ai_highlights: bool = Form(False),
    ai_dubbing: bool = Form(False),
    dub_lang: str = Form("id"),
    extract_audio: bool = Form(False),
    watermark: UploadFile = File(None),
    output_resolution: str = Form("1080p"),
    aspect_ratio: str = Form("original"),
    noise_removal: bool = Form(False),
    ai_chapters: bool = Form(False),
    social_caption: bool = Form(False),
    performance_mode: str = Form("balanced"),
):
    if not file.filename or not has_extension(file.filename, VIDEO_EXTENSIONS):
        raise HTTPException(status_code=415, detail="Unsupported video format")

    job_id = str(uuid.uuid4())[:8]
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    video_path = job_dir / safe_filename(file.filename, "input_video.mp4")
    _copy_upload(file, video_path)

    watermark_path = None
    if watermark is not None and watermark.filename:
        if not has_extension(watermark.filename, IMAGE_EXTENSIONS):
            raise HTTPException(status_code=415, detail="Unsupported watermark image format")
        watermark_path = job_dir / safe_filename(watermark.filename, "watermark.png")
        _copy_upload(watermark, watermark_path)

    config = {
        "remove_silence": remove_silence,
        "subtitle": subtitle,
        "needs_transcript": subtitle or ai_summary or ai_highlights or ai_dubbing or ai_chapters or social_caption,
        "subtitle_lang": subtitle_lang if subtitle_lang in ("id", "en") else "id",
        "ai_summary": ai_summary,
        "ai_highlights": ai_highlights,
        "ai_dubbing": ai_dubbing,
        "dub_lang": dub_lang if dub_lang in ("id", "en") else "id",
        "extract_audio": extract_audio,
        "watermark_path": str(watermark_path) if watermark_path else None,
        "output_resolution": output_resolution if output_resolution in ("720p", "1080p", "original") else "1080p",
        "aspect_ratio": aspect_ratio if aspect_ratio in ("original", "vertical") else "original",
        "noise_removal": noise_removal, "ai_chapters": ai_chapters, "social_caption": social_caption,
        "performance_mode": performance_mode,
    }

    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "step": "", "progress": 0, "cancel_requested": False, "error": None, "result": None}

    thread = threading.Thread(target=run_job, args=(job_id, video_path, config), daemon=True)
    thread.start()

    return {"job_id": job_id}


def run_job(job_id, video_path, config):
    with JOBS_LOCK:
        job = JOBS[job_id]
    out_dir = OUTPUT_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        def update(step, progress):
            job["step"], job["progress"] = step, progress
            if job.get("cancel_requested"):
                raise RuntimeError("Processing cancelled")
        job["status"] = "processing"

        update("Checking & fixing the video file", 5)
        video_path = editor.ensure_playable(video_path)

        if config["remove_silence"]:
            update("Detecting silent segments", 10)
            silences = editor.detect_silence_ranges(video_path)
            update("Removing silent segments", 15)
            trimmed_path = out_dir / "silence_removed.mp4"
            video_path = editor.remove_silence(video_path, silences, trimmed_path)

        pre_path = out_dir / "pre_processed.mp4"
        if config["watermark_path"]:
            update("Adding watermark", 20)
            editor.add_watermark(video_path, config["watermark_path"], pre_path)
        else:
            update("Saving video", 20)
            shutil.copy(video_path, pre_path)
        source_segments = []
        segments = []
        source_segments = []
        if config["needs_transcript"]:
            update("Generating transcript (speech-to-text)", 30)
            # Detect the source language automatically. The selected subtitle
            # language is an OUTPUT language, not the Whisper input language.
            lang = config.get("subtitle_lang", "id")
            task = "transcribe"
            model_size = "small" if config.get("performance_mode") == "balanced" else "medium"
            file_hash = hashlib.sha256()
            with open(pre_path, "rb") as cached_input:
                for chunk in iter(lambda: cached_input.read(1024 * 1024), b""):
                    file_hash.update(chunk)
            cache_key = hashlib.sha256(("v2-auto-source" + file_hash.hexdigest() + task + model_size).encode()).hexdigest()
            cache_path = BASE_DIR / "transcript_cache" / f"{cache_key}.json"
            cache_path.parent.mkdir(exist_ok=True)
            if cache_path.exists():
                source_segments = json.loads(cache_path.read_text(encoding="utf-8"))
            else:
                source_segments = editor.transcribe(pre_path, model_size=model_size, language=None, task=task)
                cache_path.write_text(json.dumps(source_segments), encoding="utf-8")

            # Always normalize the visible subtitle to the language selected
            # by the user. This also handles an English source video with ID
            # selected: Whisper transcribes, while the AI translates to ID.
            update(f"Translating transcript to {'Indonesian' if lang == 'id' else 'English'}", 38)
            segments = dubbing.translate_segments(source_segments, target_language=lang)

        final_path = out_dir / "final.mp4"
        if segments:
            srt_path = out_dir / "subtitle.srt"
            editor.write_srt(segments, srt_path)
            editor.write_vtt(segments, out_dir / "subtitle.vtt")
            if config["subtitle"]:
                update("Adding subtitles", 45)
                editor.burn_subtitle(pre_path, srt_path, final_path)
            else:
                shutil.copy(pre_path, final_path)
        else:
            shutil.copy(pre_path, final_path)

        if config.get("noise_removal"):
            update("Removing background noise", 50)
            clean = out_dir / "noise_removed.mp4"
            editor.remove_noise(final_path, clean)
            shutil.copy(clean, final_path)
        if config.get("aspect_ratio") != "original" or config.get("output_resolution") != "original":
            update("Applying output format", 55)
            resized = out_dir / "resized.mp4"
            editor.resize_video(final_path, resized, config["aspect_ratio"], config["output_resolution"])
            shutil.copy(resized, final_path)
        result = {"final_video": f"/download/{job_id}/final.mp4", "subtitle_srt": f"/download/{job_id}/subtitle.srt" if segments else None, "subtitle_vtt": f"/download/{job_id}/subtitle.vtt" if segments else None}

        if config["ai_summary"] and segments:
            update("AI generating summary", 65)
            result["summary"] = ai_helper.suggest_summary(segments, language=lang)

        if config["ai_highlights"] and segments:
            update("AI selecting highlight segments", 72)
            ranges = ai_helper.suggest_highlights(segments, language=lang)
            if ranges:
                highlight_path = out_dir / "highlight.mp4"
                editor.cut_highlights(final_path, ranges, highlight_path)
                result["highlight_video"] = f"/download/{job_id}/highlight.mp4"
                result["highlight_reasons"] = ranges

        if config.get("ai_chapters") and segments:
            update("AI generating chapters", 78)
            result["chapters"] = ai_helper.suggest_chapters(segments, language=lang)
        if config.get("social_caption") and segments:
            update("AI generating social caption", 82)
            result["social_caption"] = ai_helper.suggest_social_caption(segments, language=lang)

        if config["extract_audio"]:
            job["step"] = "Extracting audio from video"
            audio_path = out_dir / "audio.mp3"
            editor.extract_audio(final_path, audio_path, audio_format="mp3")
            result["audio"] = f"/download/{job_id}/audio.mp3"

        if config["ai_dubbing"] and segments:
            dub_lang = config.get("dub_lang", "id")

            # IMPORTANT: reuse the exact same `segments` (and `lang`) that
            # were already used to burn the visible subtitle above, instead
            # of re-transcribing the video from scratch. A separate
            # transcription pass can land on slightly different segment
            # boundaries/wording than the subtitle, so the spoken dub would
            # drift from what's shown on screen even in the same language.
            update(
                f"AI preparing {'Indonesian' if dub_lang == 'id' else 'English'} script for dubbing",
                85,
            )
            # Dubbing language is independent from subtitle language. Always
            # translate from the detected-language transcript to the selected
            # dubbing language, including ID -> ID normalization.
            dub_segments = dubbing.translate_segments(source_segments, target_language=dub_lang)

            update("Estimating speaker gender per segment", 87)
            genders = dubbing.detect_segment_genders(pre_path, segments)

            update("Generating dubbed voice (TTS)", 90)
            dub_tmp_dir = out_dir / "dub_tmp"
            tts_paths = dubbing.synthesize_segment_audio(
                dub_segments, dub_tmp_dir / "tts", target_language=dub_lang, genders=genders
            )

            update("Building the dubbed audio track", 94)
            total_duration = editor._probe_duration(final_path) or (
                dub_segments[-1]["end"] if dub_segments else 0
            )
            dubbed_track_path = out_dir / "dubbed_track.mp3"
            dubbing.build_dubbed_track(
                dub_segments, tts_paths, total_duration, dub_tmp_dir, dubbed_track_path
            )

            update("Muxing dubbed audio onto video", 97)
            dubbed_video_path = out_dir / "dubbed.mp4"
            dubbing.mux_dubbed_audio(final_path, dubbed_track_path, dubbed_video_path)
            result["dubbed_video"] = f"/download/{job_id}/dubbed.mp4"
            result["dubbed_lang"] = dub_lang

        job["result"] = result
        job["status"] = "done"
        job["step"], job["progress"] = "Done", 100

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[job {job_id}] FAILED at step '{job['step']}':\n{tb}", flush=True)
        job["status"] = "cancelled" if job.get("cancel_requested") else "error"
        job["error"] = f"{e} (step: {job['step']})"


@app.post("/cancel/{job_id}")
def cancel(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return JSONResponse({"error": "job not found"}, status_code=404)
        job["cancel_requested"] = True
        job["status"] = "cancelling"
    return {"ok": True}


@app.get("/status/{job_id}")
def status(job_id: str):
    if not is_valid_job_id(job_id):
        return JSONResponse({"error": "job not found"}, status_code=404)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return job


@app.get("/download/{job_id}/{filename}")
def download(job_id: str, filename: str):
    if not is_valid_job_id(job_id) or not is_safe_output_name(filename):
        return JSONResponse({"error": "file not found"}, status_code=404)
    path = OUTPUT_DIR / job_id / filename
    if not path.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)
    return FileResponse(str(path), filename=filename)
