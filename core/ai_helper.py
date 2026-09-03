"""
AI helper, using the standard OpenAI-compatible chat completions API -- NOT
tied/defaulted to any specific provider. Any provider compatible with the
OpenAI format can be used (Cerebras, Groq, OpenRouter, Together.ai, etc.)
just by setting the 3 env vars below -- there's no built-in provider in
this code, so switching providers = switching env vars, no code changes
needed:

- suggest_title_caption : generate a title, caption, and hashtags from the transcript
- suggest_highlights    : pick the most interesting parts of the transcript for a highlight reel

Env vars that are REQUIRED (no defaults -- if empty, a clear error is raised):
- AI_API_KEY   -- API key from the provider you're using
- AI_BASE_URL  -- the provider's API endpoint (must be OpenAI-compatible)
- AI_MODEL     -- the model name/slug at that provider

Example OpenAI-compatible combinations (just fill in the 3 env vars for
whichever provider you want to use at the time):
- Cerebras   : AI_BASE_URL=https://api.cerebras.ai/v1        (cloud.cerebras.ai)
- Groq       : AI_BASE_URL=https://api.groq.com/openai/v1    (console.groq.com)
- OpenRouter : AI_BASE_URL=https://openrouter.ai/api/v1      (openrouter.ai)
- Together   : AI_BASE_URL=https://api.together.xyz/v1
"""

import json
import os

# Some models (e.g. gpt-oss, zai-glm) are "reasoning models" -- they "think"
# first (chain-of-thought) before giving a final answer, and that consumes
# tokens. If max_tokens runs out while still in the thinking stage, what
# comes back is just a fragment of the reasoning (not the answer) -- hence
# the need for a low reasoning_effort (keep the thinking brief) + a looser
# max_tokens as a safety net.
REASONING_MODEL_KEYWORDS = ("gpt-oss", "glm", "qwen3", "qwen-3", "deepseek-r1")


def _extra_kwargs_for_model(model_name):
    if any(k in model_name.lower() for k in REASONING_MODEL_KEYWORDS):
        return {"reasoning_effort": "low"}
    return {}


def _get_config():
    """Read the AI provider config from env vars. No default/built-in
    provider -- everything must be set via .env / docker-compose environment."""
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
            f"Env var(s) {', '.join(missing)} not set -- the AI features "
            "require all three (AI_API_KEY, AI_BASE_URL, AI_MODEL) to be "
            "set for whichever AI provider you want to use. There is no "
            "default provider; see the comments at the top of "
            "core/ai_helper.py for examples of compatible providers."
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
    If the JSON got cut off (e.g. because a free provider truncated the
    output midway), try to extract whatever title/caption/hashtags are
    already complete via regex, instead of returning nothing at all.
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

    prompt = f"""Here is the transcript of a video:

{transcript}

Create the following for the {platform} platform:
1. An engaging video title (max 10 words)
2. A short caption for the post (2-3 sentences)
3. 5 relevant hashtags

Reply ONLY in the following JSON format, with no other text:
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
                "  [ai_helper] AI response was truncated (provider/model token limit). "
                "Attempting to salvage a partial result...",
                flush=True,
            )
        return _salvage_title_caption(text)


def suggest_highlights(segments, max_highlights=3, target_total_sec=45):
    """
    Return list of {start, end, reason} - the most interesting parts to use
    for a highlight reel, sorted by their time of appearance.
    """
    api_key, base_url, model = _get_config()
    client = _client(base_url, api_key)
    transcript = _segments_to_text(segments, with_timestamps=True)
    if not transcript.strip():
        return []

    prompt = f"""Here is the full video transcript with timestamps (seconds):

{transcript}

Pick at most {max_highlights} of the most interesting/important parts to use for a short
highlight video, with a total highlight duration of around {target_total_sec} seconds. Only
use timestamps that actually appear in the transcript above.

Reply ONLY in the following JSON array format, with no other text:
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
                "  [ai_helper] AI response (highlights) was truncated (provider/model "
                "token limit). Attempting to salvage the objects that are already complete...",
                flush=True,
            )
        # Salvage any complete {start,end,reason} objects that came before the cutoff
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
