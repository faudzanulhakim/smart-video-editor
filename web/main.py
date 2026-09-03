"""
Auto Video Editor web app.
Run with: uvicorn web.main:app --host 0.0.0.0 --port 8000
"""

import shutil
import sys
import threading
import traceback
import uuid
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.append(str(Path(__file__).resolve().parent.parent))
from core import editor, ai_helper

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Auto Video Editor")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# Job status is stored in-memory (sufficient for single-instance / MVP)
JOBS = {}


@app.get("/")
def index():
    return FileResponse(str(Path(__file__).parent / "static" / "index.html"))


@app.post("/upload")
def upload(
    file: UploadFile = File(...),
    remove_silence: bool = Form(False),
    subtitle: bool = Form(False),
    subtitle_lang: str = Form("id"),
    ai_title_caption: bool = Form(False),
    ai_highlights: bool = Form(False),
    extract_audio: bool = Form(False),
    watermark: UploadFile = File(None),
):
    job_id = str(uuid.uuid4())[:8]
    job_dir = UPLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    video_path = job_dir / file.filename
    with open(video_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    watermark_path = None
    if watermark is not None and watermark.filename:
        watermark_path = job_dir / watermark.filename
        with open(watermark_path, "wb") as f:
            shutil.copyfileobj(watermark.file, f)

    config = {
        "remove_silence": remove_silence,
        "subtitle": subtitle or ai_title_caption or ai_highlights,  # needs a transcript
        "subtitle_lang": subtitle_lang if subtitle_lang in ("id", "en") else "id",
        "ai_title_caption": ai_title_caption,
        "ai_highlights": ai_highlights,
        "extract_audio": extract_audio,
        "watermark_path": str(watermark_path) if watermark_path else None,
    }

    JOBS[job_id] = {"status": "queued", "step": "", "error": None, "result": None}

    thread = threading.Thread(target=run_job, args=(job_id, video_path, config), daemon=True)
    thread.start()

    return {"job_id": job_id}


def run_job(job_id, video_path, config):
    job = JOBS[job_id]
    out_dir = OUTPUT_DIR / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        job["status"] = "processing"

        job["step"] = "Checking & fixing the video file"
        video_path = editor.ensure_playable(video_path)

        if config["remove_silence"]:
            job["step"] = "Detecting silent segments"
            silences = editor.detect_silence_ranges(video_path)
            job["step"] = "Removing silent segments"
            trimmed_path = out_dir / "silence_removed.mp4"
            video_path = editor.remove_silence(video_path, silences, trimmed_path)

        pre_path = out_dir / "pre_processed.mp4"
        if config["watermark_path"]:
            job["step"] = "Adding watermark"
            editor.add_watermark(video_path, config["watermark_path"], pre_path)
        else:
            job["step"] = "Saving video"
            shutil.copy(video_path, pre_path)

        segments = []
        if config["subtitle"]:
            job["step"] = "Generating transcript (speech-to-text)"
            lang = config.get("subtitle_lang", "id")
            task = "translate" if lang == "en" else "transcribe"
            # language="id" is used as a hint for the SOURCE audio language
            # (the original video is assumed to be Indonesian) -- useful for
            # both the "transcribe" task (ID text output) and the
            # "translate" task (Whisper still needs to know the source
            # language, but the output is always translated to English).
            segments = editor.transcribe(pre_path, language="id", task=task)

        final_path = out_dir / "final.mp4"
        if config["subtitle"] and segments:
            job["step"] = "Adding subtitles"
            srt_path = out_dir / "subtitle.srt"
            editor.write_srt(segments, srt_path)
            editor.burn_subtitle(pre_path, srt_path, final_path)
        else:
            shutil.copy(pre_path, final_path)

        result = {"final_video": f"/download/{job_id}/final.mp4"}

        if config["ai_title_caption"] and segments:
            job["step"] = "AI generating title & caption"
            result["title_caption"] = ai_helper.suggest_title_caption(segments)

        if config["ai_highlights"] and segments:
            job["step"] = "AI selecting highlight segments"
            ranges = ai_helper.suggest_highlights(segments)
            if ranges:
                highlight_path = out_dir / "highlight.mp4"
                editor.cut_highlights(final_path, ranges, highlight_path)
                result["highlight_video"] = f"/download/{job_id}/highlight.mp4"
                result["highlight_reasons"] = ranges

        if config["extract_audio"]:
            job["step"] = "Extracting audio from video"
            audio_path = out_dir / "audio.mp3"
            editor.extract_audio(final_path, audio_path, audio_format="mp3")
            result["audio"] = f"/download/{job_id}/audio.mp3"

        job["result"] = result
        job["status"] = "done"
        job["step"] = "Done"

    except Exception as e:
        tb = traceback.format_exc()
        print(f"[job {job_id}] FAILED at step '{job['step']}':\n{tb}", flush=True)
        job["status"] = "error"
        job["error"] = f"{e} (step: {job['step']})"


@app.get("/status/{job_id}")
def status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return job


@app.get("/download/{job_id}/{filename}")
def download(job_id: str, filename: str):
    path = OUTPUT_DIR / job_id / filename
    if not path.exists():
        return JSONResponse({"error": "file not found"}, status_code=404)
    return FileResponse(str(path), filename=filename)
