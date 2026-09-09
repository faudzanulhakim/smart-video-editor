"""
AI helper, using the standard OpenAI-compatible chat completions API -- NOT
tied/defaulted to any specific provider. Any provider compatible with the
OpenAI format can be used (Cerebras, Groq, OpenRouter, Together.ai, etc.)
just by setting the 3 env vars below -- there's no built-in provider in
this code, so switching providers = switching env vars, no code changes
needed:

- suggest_summary       : generate a concise summary from the transcript
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


def _salvage_summary(text):
    """
    If the JSON got cut off, salvage the summary text instead of returning nothing.
    """
    import re
    summary = re.search(r'"summary"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    return summary.group(1) if summary else text[:1000]


LANGUAGE_NAMES = {"id": "Indonesian", "en": "English"}

# When using a router like OpenRouter's "openrouter/free" (AI_MODEL=openrouter/free),
# each request may land on a different underlying free model chosen at random.
# Occasionally that means landing on a model unsuited for this task (a coding-only
# model, a safety/moderation classifier, etc.), which returns garbage instead of
# the requested JSON. Retrying re-rolls the router's model choice, so a few retries
# meaningfully increase the odds of getting a usable response without having to
# pin to one specific model (which would trade away the router's pooled rate limit).
MAX_AI_RETRIES = 3


def _looks_like_valid_response(parsed):
    """Loose sanity check that the parsed JSON actually looks like what we asked
    for, not just valid JSON that happens to be something unrelated (e.g. a
    moderation classifier replying {"safe": true})."""
    if isinstance(parsed, dict):
        return "summary" in parsed
    if isinstance(parsed, list):
        return True  # empty list is a valid (if unlikely) highlight result
    return False


def suggest_summary(segments, platform="general", language="id"):
    """Return a concise summary of the video transcript.

    `language` controls the language of the generated summary
    ("id" or "en") -- this should match the subtitle language chosen by the
    user, NOT necessarily the language of the prompt/code itself.
    """
    api_key, base_url, model = _get_config()
    client = _client(base_url, api_key)
    transcript = _segments_to_text(segments, with_timestamps=False)
    if not transcript.strip():
        return ""

    lang_name = LANGUAGE_NAMES.get(language, "Indonesian")

    prompt = f"""Here is the transcript of a video:

{transcript}

    Create a concise, informative summary of this video in 3-5 sentences.
Write the summary in {lang_name}, regardless of what
language the transcript above is in.

Reply ONLY in the following JSON format, with no other text:
{{"summary": "..."}}"""

    last_text = ""
    last_resp = None
    for attempt in range(1, MAX_AI_RETRIES + 1):
        resp = client.chat.completions.create(
            model=model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
            **_extra_kwargs_for_model(model),
        )
        text = _strip_json_fences(_extract_text(resp))
        last_text, last_resp = text, resp
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None

        if parsed is not None and _looks_like_valid_response(parsed):
            return str(parsed.get("summary", "")).strip()

        print(
            f"  [ai_helper] Attempt {attempt}/{MAX_AI_RETRIES} returned an unusable "
            f"response (likely a mismatched model from the router): {text[:120]!r} "
            f"{'-- retrying with a new model...' if attempt < MAX_AI_RETRIES else '-- giving up.'}",
            flush=True,
        )

    if _was_truncated(last_resp):
        print(
            "  [ai_helper] AI response was truncated (provider/model token limit). "
            "Attempting to salvage a partial result...",
            flush=True,
        )
    return _salvage_summary(last_text)


def suggest_highlights(segments, max_highlights=3, target_total_sec=45, language="id"):
    """
    Return list of {start, end, reason} - the most interesting parts to use
    for a highlight reel, sorted by their time of appearance.

    `language` controls the language of the "reason" text ("id" or "en") --
    this should match the subtitle language chosen by the user.
    """
    api_key, base_url, model = _get_config()
    client = _client(base_url, api_key)
    transcript = _segments_to_text(segments, with_timestamps=True)
    if not transcript.strip():
        return []

    lang_name = LANGUAGE_NAMES.get(language, "Indonesian")

    prompt = f"""Here is the full video transcript with timestamps (seconds):

{transcript}

Pick at most {max_highlights} of the most interesting/important parts to use for a short
highlight video, with a total highlight duration of around {target_total_sec} seconds. Only
use timestamps that actually appear in the transcript above.

Write the "reason" field in {lang_name}, regardless of what language the
transcript above is in.

Reply ONLY in the following JSON array format, with no other text:
[{{"start": 12.5, "end": 20.0, "reason": "..."}}, ...]"""

    last_text = ""
    last_resp = None
    for attempt in range(1, MAX_AI_RETRIES + 1):
        resp = client.chat.completions.create(
            model=model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
            **_extra_kwargs_for_model(model),
        )
        text = _strip_json_fences(_extract_text(resp))
        last_text, last_resp = text, resp
        try:
            parsed = json.loads(text)
            parsed.sort(key=lambda r: r["start"])
        except (json.JSONDecodeError, KeyError, TypeError):
            parsed = None

        if parsed is not None and _looks_like_valid_response(parsed):
            return parsed

        print(
            f"  [ai_helper] Attempt {attempt}/{MAX_AI_RETRIES} returned an unusable "
            f"response (likely a mismatched model from the router): {text[:120]!r} "
            f"{'-- retrying with a new model...' if attempt < MAX_AI_RETRIES else '-- giving up.'}",
            flush=True,
        )

    if _was_truncated(last_resp):
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
        last_text,
    ):
        salvaged.append({
            "start": float(m.group(1)),
            "end": float(m.group(2)),
            "reason": m.group(3),
        })
    salvaged.sort(key=lambda r: r["start"])
    return salvaged


def suggest_chapters(segments, language="id"):
    api_key, base_url, model = _get_config()
    client = _client(base_url, api_key)
    transcript = _segments_to_text(segments, with_timestamps=True)
    prompt = f"Create concise video chapters from this timestamped transcript. Return ONLY JSON array of objects with title and start (seconds), for example [{{'title': 'Introduction', 'start': 0}}]. Use {LANGUAGE_NAMES.get(language, 'Indonesian')}. Do not use markdown or any text outside JSON.\n{transcript}"
    last_text = ""
    for attempt in range(1, MAX_AI_RETRIES + 1):
        resp = client.chat.completions.create(model=model, max_tokens=1200, messages=[{"role": "user", "content": prompt}], **_extra_kwargs_for_model(model))
        last_text = _strip_json_fences(_extract_text(resp))
        try:
            parsed = json.loads(last_text)
            if isinstance(parsed, dict):
                parsed = parsed.get("chapters", parsed.get("items", []))
            if isinstance(parsed, list):
                valid = []
                for item in parsed:
                    if not isinstance(item, dict):
                        continue
                    title = item.get("title") or item.get("name") or item.get("chapter") or item.get("heading")
                    raw_start = item.get("start")
                    if raw_start is None:
                        raw_start = item.get("timestamp", item.get("start_time", item.get("startTime")))
                    if not title or raw_start is None:
                        continue
                    try:
                        if isinstance(raw_start, str) and ":" in raw_start:
                            parts = [float(part) for part in raw_start.strip().split(":")]
                            if len(parts) == 2:
                                start = parts[0] * 60 + parts[1]
                            elif len(parts) == 3:
                                start = parts[0] * 3600 + parts[1] * 60 + parts[2]
                            else:
                                continue
                        else:
                            start = float(raw_start)
                        if start < 0:
                            continue
                    except (TypeError, ValueError):
                        continue
                    valid.append({"start": start, "title": str(title).strip()})
                if valid:
                    return sorted(valid, key=lambda item: item["start"])
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            pass
        print(f"  [ai_helper] Chapters attempt {attempt}/{MAX_AI_RETRIES} returned invalid JSON: {last_text[:120]!r}", flush=True)
    return []


def suggest_social_caption(segments, language="id"):
    api_key, base_url, model = _get_config()
    client = _client(base_url, api_key)
    transcript = _segments_to_text(segments, with_timestamps=False)
    prompt = f"Create a social media caption for this video in {LANGUAGE_NAMES.get(language, 'Indonesian')}. Return ONLY JSON with caption, hashtags (array), and hook.\n{transcript}"
    for attempt in range(1, MAX_AI_RETRIES + 1):
        resp = client.chat.completions.create(model=model, max_tokens=800, messages=[{"role": "user", "content": prompt}], **_extra_kwargs_for_model(model))
        text = _strip_json_fences(_extract_text(resp))
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and isinstance(parsed.get("caption"), str) and parsed["caption"].strip():
                hashtags = parsed.get("hashtags", [])
                if not isinstance(hashtags, list):
                    hashtags = [str(hashtags)]
                return {
                    "caption": parsed["caption"].strip(),
                    "hook": str(parsed.get("hook", "")).strip(),
                    "hashtags": [str(tag).strip() for tag in hashtags if str(tag).strip()],
                }
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        print(f"  [ai_helper] Caption attempt {attempt}/{MAX_AI_RETRIES} returned invalid JSON: {text[:120]!r}", flush=True)
    return {"caption": "", "hashtags": [], "hook": ""}
