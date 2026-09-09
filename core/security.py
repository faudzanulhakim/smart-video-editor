"""Small, dependency-free validation helpers used by the web app."""

import re
from pathlib import Path


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
JOB_ID_PATTERN = re.compile(r"^[a-f0-9]{8}$")


def safe_filename(filename, default_name):
    """Return only a safe basename, falling back when the upload has no name."""
    name = Path(filename or "").name
    if not name or name in {".", ".."}:
        return default_name
    return name


def is_valid_job_id(job_id):
    return bool(JOB_ID_PATTERN.fullmatch(job_id or ""))


def is_safe_output_name(filename):
    """Allow only a single, non-hidden output filename."""
    name = Path(filename or "")
    return bool(name.name == filename and filename not in {"", ".", ".."} and not filename.startswith("."))


def has_extension(filename, extensions):
    return Path(filename).suffix.lower() in extensions