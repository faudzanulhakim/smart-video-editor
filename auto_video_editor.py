#!/usr/bin/env python3
"""
Auto Video Editor - CLI / Batch mode
======================================
For the web app with an upload UI + AI features, run it via Docker (see README.md).

CLI usage:
    python auto_video_editor.py --input ./raw_videos --output ./processed_videos --config config.json
"""

import argparse
import json
import sys
from pathlib import Path

from core.editor import (
    VIDEO_EXTS,
    VideoFileClip,
    detect_silence_ranges,
    remove_silence,
    transcribe,
    write_srt,
    burn_subtitle,
    add_intro_outro,
    add_watermark,
    ensure_playable,
    extract_audio,
)


def process_video(input_path, output_dir, config):
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = output_dir / "_tmp"
    tmp_dir.mkdir(exist_ok=True)
    orig_stem = input_path.stem  # used for the final filename, regardless of any stage

    print(f"\n=== Processing: {input_path.name} ===")

    print("  - Checking & fixing video metadata (if needed)...")
    input_path = ensure_playable(input_path)

    if config.get("remove_silence", {}).get("enabled", False):
        print("  - Detecting & removing silent segments...")
        sc = config["remove_silence"]
        silences = detect_silence_ranges(
            input_path,
            silence_thresh_db=sc.get("threshold_db", -35),
            min_silence_len=sc.get("min_len_sec", 0.6),
        )
        trimmed_path = tmp_dir / f"{input_path.stem}_trimmed.mp4"
        input_path = remove_silence(
            input_path, silences, trimmed_path, padding=sc.get("padding_sec", 0.15)
        )

    io_cfg = config.get("intro_outro", {})
    wm_cfg = config.get("watermark", {})

    pre_wm_path = tmp_dir / f"{input_path.stem}_pre_wm.mp4"

    if io_cfg.get("enabled", False):
        print("  - Adding intro/outro...")
        clip = VideoFileClip(str(input_path))
        clip = add_intro_outro(clip, io_cfg.get("intro_path"), io_cfg.get("outro_path"))
        clip.write_videofile(
            str(pre_wm_path), codec="libx264", audio_codec="aac",
            preset="veryfast", logger=None,
        )
        clip.close()
        input_path = pre_wm_path

    pre_sub_path = tmp_dir / f"{input_path.stem}_pre_sub.mp4"
    if wm_cfg.get("enabled", False):
        print("  - Adding watermark...")
        input_path = add_watermark(
            input_path,
            wm_cfg.get("image_path"),
            pre_sub_path,
            position=wm_cfg.get("position", "bottom-right"),
            opacity=wm_cfg.get("opacity", 0.7),
            width_ratio=wm_cfg.get("width_ratio", 0.15),
        )
    else:
        import shutil as _shutil
        _shutil.copy(input_path, pre_sub_path)
        input_path = pre_sub_path

    final_path = output_dir / f"{orig_stem}_final.mp4"

    sub_cfg = config.get("subtitle", {})
    if sub_cfg.get("enabled", False):
        print("  - Generating automatic subtitles (speech-to-text)...")
        lang = sub_cfg.get("output_language", "id")
        task = "translate" if lang == "en" else "transcribe"
        segments = transcribe(
            pre_sub_path,
            model_size=sub_cfg.get("model_size", "medium"),
            language=sub_cfg.get("language", "id"),
            task=task,
        )
        srt_path = tmp_dir / f"{input_path.stem}.srt"
        write_srt(segments, srt_path)
        print("  - Burning subtitles into the video...")
        burn_subtitle(pre_sub_path, srt_path, final_path, style=sub_cfg.get("style"))
    else:
        pre_sub_path.rename(final_path)

    audio_cfg = config.get("extract_audio", {})
    if audio_cfg.get("enabled", False):
        print("  - Extracting audio from video...")
        audio_format = audio_cfg.get("format", "mp3")
        audio_path = output_dir / f"{orig_stem}_audio.{audio_format}"
        extract_audio(final_path, audio_path, audio_format=audio_format)
        print(f"  Audio -> {audio_path}")

    print(f"  Done -> {final_path}")
    return final_path


def batch_process(input_dir, output_dir, config):
    input_dir = Path(input_dir)
    videos = sorted(p for p in input_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS)
    if not videos:
        print(f"No video files found in {input_dir}")
        return

    print(f"Found {len(videos)} video(s). Starting batch processing...")
    for v in videos:
        try:
            process_video(v, output_dir, config)
        except Exception as e:
            print(f"  FAILED to process {v.name}: {e}")


def main():
    parser = argparse.ArgumentParser(description="Auto Video Editor - automated video editing (CLI)")
    parser.add_argument("--input", required=True, help="Folder containing raw videos")
    parser.add_argument("--output", required=True, help="Folder for the resulting videos")
    parser.add_argument("--config", default="config.json", help="Path to the JSON config file")
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(f"Config file '{args.config}' not found.")
        sys.exit(1)

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    batch_process(args.input, args.output, config)


if __name__ == "__main__":
    main()
