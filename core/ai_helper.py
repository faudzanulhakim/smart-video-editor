"""
Bantuan AI, pakai standar OpenAI-compatible chat completions API -- TIDAK
terikat/default ke provider mana pun. Semua provider yang kompatibel format
OpenAI bisa dipakai (Cerebras, Groq, OpenRouter, Together.ai, dll) hanya
dengan mengatur 3 env var di bawah -- tidak ada provider bawaan di kode ini,
jadi ganti provider = ganti env var, tanpa sentuh kode sama sekali:

- suggest_title_caption : buat judul, caption, dan hashtag dari transkrip
- suggest_highlights    : pilih bagian paling menarik dari transkrip untuk dijadikan highlight reel

Env var yang WAJIB diisi (tidak ada default -- kalau kosong akan error jelas):
- AI_API_KEY   -- API key dari provider yang kamu pakai
- AI_BASE_URL  -- endpoint API provider (harus OpenAI-compatible)
- AI_MODEL     -- nama/slug model di provider tsb

Contoh kombinasi yang OpenAI-compatible (tinggal isi 3 env var sesuai provider
yang kamu mau pakai saat itu):
- Cerebras   : AI_BASE_URL=https://api.cerebras.ai/v1        (cloud.cerebras.ai)
- Groq       : AI_BASE_URL=https://api.groq.com/openai/v1    (console.groq.com)
- OpenRouter : AI_BASE_URL=https://openrouter.ai/api/v1      (openrouter.ai)
- Together   : AI_BASE_URL=https://api.together.xyz/v1
"""

import json
import os

# Beberapa model (mis. gpt-oss, zai-glm) adalah "reasoning model" -- mereka
# "mikir" dulu (chain-of-thought) sebelum kasih jawaban final, dan itu makan
# token. Kalau max_tokens kehabisan saat masih di tahap mikir, yang balik cuma
# potongan reasoning-nya (bukan jawaban) -- makanya perlu reasoning_effort
# rendah (ringkas mikirnya) + max_tokens dilonggarkan sebagai jaring pengaman.
REASONING_MODEL_KEYWORDS = ("gpt-oss", "glm", "qwen3", "qwen-3", "deepseek-r1")


def _extra_kwargs_for_model(model_name):
    if any(k in model_name.lower() for k in REASONING_MODEL_KEYWORDS):
        return {"reasoning_effort": "low"}
    return {}


def _get_config():
    """Baca konfigurasi provider AI dari env var. Tidak ada default/provider
    bawaan -- semua wajib diisi lewat .env / docker-compose environment."""
    api_key = os.environ.get("AI_API_KEY")
    base_url = os.environ.get("AI_BASE_URL")
    model = os.environ.get("AI_MODEL")

    missing = [
        name for name, val in
        [("AI_API_KEY", api_key), ("AI_BASE_URL", base_url), ("AI_MODEL", model)]
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Env var {', '.join(missing)} belum diset -- fitur AI butuh "
            "ketiganya diisi (AI_API_KEY, AI_BASE_URL, AI_MODEL) sesuai "
            "provider AI yang mau kamu pakai. Tidak ada provider default; "
            "cek komentar di atas core/ai_helper.py untuk contoh beberapa "
            "provider yang kompatibel."
        )
    return api_key, base_url, model


def _client(base_url, api_key):
    from openai import OpenAI
    return OpenAI(base_url=base_url, api_key=api_key)


def _segments_to_text(segments, with_timestamps=True):
    lines = []
    for seg in segments:
        if with_timestamps:
            lines.append(f"[{seg['start']:.1f}-{seg['end']:.1f}] {seg['text']}")
        else:
            lines.append(seg["text"])
    return "\n".join(lines)


def _extract_text(resp):
    return (resp.choices[0].message.content or "").strip()


def _was_truncated(resp):
    try:
        return resp.choices[0].finish_reason == "length"
    except (AttributeError, IndexError):
        return False


def _strip_json_fences(text):
    return text.replace("```json", "").replace("```", "").strip()


def _salvage_title_caption(text):
    """
    Kalau JSON kepotong (mis. karena provider gratis motong output di tengah
    jalan), coba ekstrak title/caption/hashtags yang SUDAH lengkap lewat
    regex, daripada balikin kosong total.
    """
    import re
    title = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    caption = re.search(r'"caption"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    hashtags = re.findall(r'"(#?[A-Za-z0-9_]+)"', text.split('"hashtags"')[-1]) \
        if '"hashtags"' in text else []
    return {
        "title": title.group(1) if title else "",
        "caption": caption.group(1) if caption else text[:300],
        "hashtags": [h if h.startswith("#") else f"#{h}" for h in hashtags],
    }


def suggest_title_caption(segments, platform="general"):
    """Return dict: {title, caption, hashtags: [...]}"""
    api_key, base_url, model = _get_config()
    client = _client(base_url, api_key)
    transcript = _segments_to_text(segments, with_timestamps=False)
    if not transcript.strip():
        return {"title": "", "caption": "", "hashtags": []}

    prompt = f"""Berikut transkrip sebuah video:

{transcript}

Buatkan untuk platform {platform}:
1. Judul video yang menarik (maks 10 kata)
2. Caption singkat untuk posting (2-3 kalimat)
3. 5 hashtag relevan

Jawab HANYA dalam format JSON seperti ini, tanpa teks lain:
{{"title": "...", "caption": "...", "hashtags": ["...", "..."]}}"""

    resp = client.chat.completions.create(
        model=model,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
        **_extra_kwargs_for_model(model),
    )
    text = _strip_json_fences(_extract_text(resp))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if _was_truncated(resp):
            print(
                "  [ai_helper] Respons AI kepotong (limit token provider/model). "
                "Mencoba selamatkan sebagian hasil...",
                flush=True,
            )
        return _salvage_title_caption(text)


def suggest_highlights(segments, max_highlights=3, target_total_sec=45):
    """
    Return list of {start, end, reason} - bagian paling menarik untuk highlight reel,
    diurutkan berdasarkan waktu kemunculan.
    """
    api_key, base_url, model = _get_config()
    client = _client(base_url, api_key)
    transcript = _segments_to_text(segments, with_timestamps=True)
    if not transcript.strip():
        return []

    prompt = f"""Berikut transkrip video lengkap dengan timestamp (detik):

{transcript}

Pilih maksimal {max_highlights} bagian paling menarik/penting untuk dijadikan video highlight
singkat, total durasi highlight sekitar {target_total_sec} detik. Gunakan timestamp yang
benar-benar ada di transkrip di atas.

Jawab HANYA dalam format JSON array seperti ini, tanpa teks lain:
[{{"start": 12.5, "end": 20.0, "reason": "..."}}, ...]"""

    resp = client.chat.completions.create(
        model=model,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
        **_extra_kwargs_for_model(model),
    )
    text = _strip_json_fences(_extract_text(resp))
    try:
        ranges = json.loads(text)
        ranges.sort(key=lambda r: r["start"])
        return ranges
    except (json.JSONDecodeError, KeyError, TypeError):
        if _was_truncated(resp):
            print(
                "  [ai_helper] Respons AI (highlights) kepotong (limit token "
                "provider/model). Mencoba selamatkan objek yang sudah lengkap...",
                flush=True,
            )
        # Selamatkan objek {start,end,reason} yang sudah utuh sebelum titik potong
        import re
        salvaged = []
        for m in re.finditer(
            r'\{\s*"start"\s*:\s*([\d.]+)\s*,\s*"end"\s*:\s*([\d.]+)\s*,\s*"reason"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}',
            text,
        ):
            salvaged.append({
                "start": float(m.group(1)),
                "end": float(m.group(2)),
                "reason": m.group(3),
            })
        salvaged.sort(key=lambda r: r["start"])
        return salvaged
