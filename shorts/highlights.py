"""AI highlight selection: transcript -> scored, sentence-aligned clip segments.

The OpusClip-style "intelligence front half". One Claude call per video:
the full-video SRT cues are numbered and sent to the model, which picks the
strongest standalone moments and returns them as *cue index ranges* — so cut
points always land on speech boundaries — plus a title, a hook line for the
text overlay, caption keywords, and a 0-99 virality score broken into
hook / flow / value / trend (OpusClip's rubric).

Requires ANTHROPIC_API_KEY (or any credential source the anthropic SDK
resolves). Callers should treat failures as non-fatal and fall back to the
duration strategy.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get("HIGHLIGHTS_MODEL", "claude-opus-4-8")
DEFAULT_MAX_CLIPS = 5
DEFAULT_MIN_CLIP_S = 15.0
DEFAULT_MAX_CLIP_S = 65.0

_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "integer", "description": "0-99: does the clip's opening grab attention?"},
        "flow": {"type": "integer", "description": "0-99: logical progression with a satisfying conclusion?"},
        "value": {"type": "integer", "description": "0-99: emotional resonance / informational value?"},
        "trend": {"type": "integer", "description": "0-99: alignment with current audience interests?"},
        "overall": {"type": "integer", "description": "0-99 overall virality score"},
    },
    "required": ["hook", "flow", "value", "trend", "overall"],
    "additionalProperties": False,
}

HIGHLIGHTS_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_cue": {"type": "integer", "description": "index of the first cue in the clip"},
                    "end_cue": {"type": "integer", "description": "index of the last cue in the clip (inclusive)"},
                    "title": {"type": "string", "description": "clip title, max ~60 chars"},
                    "hook_line": {"type": "string", "description": "short punchy overlay text for the first seconds, max 8 words"},
                    "keywords": {
                        "type": "array", "items": {"type": "string"},
                        "description": "3-5 emotionally/semantically loaded words from the clip's speech, for caption emphasis",
                    },
                    "virality": _SCORE_SCHEMA,
                    "reason": {"type": "string", "description": "one sentence: why this moment works standalone"},
                },
                "required": ["start_cue", "end_cue", "title", "hook_line",
                             "keywords", "virality", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["clips"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are an expert short-form video editor. You turn long-form narration videos
into standalone vertical shorts for TikTok / Reels / YouTube Shorts.

You receive a numbered transcript (cue index, start-end seconds, text). Select
the moments most likely to hold attention as STANDALONE clips.

Rules:
- Each clip is a contiguous cue range [start_cue, end_cue] (inclusive).
- A clip must begin at the start of a sentence or thought and end at a natural
  conclusion — never mid-sentence.
- The first ~3 seconds decide whether viewers keep watching: prefer clips that
  open on a striking claim, question, number, or emotional beat.
- Clips must not overlap. Prefer variety across the video over adjacent picks.
- Score each clip 0-99 on: hook (opening grabs attention), flow (logical arc,
  satisfying end), value (emotional/informational payoff), trend (alignment
  with what audiences currently engage with), and overall.
- hook_line: max 8 words, punchy, curiosity-driving, plain language, no quotes
  or emoji. It is burned on screen over the clip's opening.
- keywords: 3-5 single words that appear in the clip's speech and carry its
  emotional or informational weight."""


class HighlightError(Exception):
    pass


def _format_transcript(cues: List[Dict[str, Any]]) -> str:
    return "\n".join(
        f"[{i}] {c['start_ms'] / 1000:.2f}-{c['end_ms'] / 1000:.2f}: {c['text']}"
        for i, c in enumerate(cues)
    )


def select_highlights(
    cues: List[Dict[str, Any]],
    max_clips: int = DEFAULT_MAX_CLIPS,
    min_clip_s: float = DEFAULT_MIN_CLIP_S,
    max_clip_s: float = DEFAULT_MAX_CLIP_S,
    project_title: str = "",
    model: str = DEFAULT_MODEL,
    client: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Return clip segments sorted by virality score (best first).

    Each segment: {part_number, title, hook_line, keywords, virality, reason,
    start_s, end_s, duration_s, start_cue, end_cue}.
    """
    if not cues:
        raise HighlightError("no transcript cues to select from")

    import anthropic  # lazy: other segment strategies must work without it

    client = client or anthropic.Anthropic()

    duration_s = cues[-1]["end_ms"] / 1000
    user_prompt = (
        f"Video: {project_title or 'untitled'} — {duration_s:.0f}s total, "
        f"{len(cues)} transcript cues.\n"
        f"Select up to {max_clips} clips, each {min_clip_s:.0f}-{max_clip_s:.0f} "
        f"seconds long.\n\nTranscript:\n{_format_transcript(cues)}"
    )

    response = client.messages.create(
        model=model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": HIGHLIGHTS_SCHEMA}},
        messages=[{"role": "user", "content": user_prompt}],
    )
    if response.stop_reason not in ("end_turn", "stop_sequence"):
        raise HighlightError(f"unexpected stop_reason: {response.stop_reason}")

    text = next((b.text for b in response.content if b.type == "text"), "")
    clips = json.loads(text).get("clips", [])
    logger.info("model proposed %d clips (usage: %s in / %s out)",
                len(clips), response.usage.input_tokens, response.usage.output_tokens)

    segments = _validate_clips(clips, cues, min_clip_s, max_clip_s)
    if not segments:
        raise HighlightError("no valid clips after validation")

    segments.sort(key=lambda s: -s["virality"]["overall"])
    for rank, seg in enumerate(segments[:max_clips], start=1):
        seg["part_number"] = rank
    return segments[:max_clips]


def _validate_clips(
    clips: List[Dict[str, Any]],
    cues: List[Dict[str, Any]],
    min_clip_s: float,
    max_clip_s: float,
) -> List[Dict[str, Any]]:
    """Map cue ranges to seconds; drop out-of-range, overlong, or overlapping clips."""
    out: List[Dict[str, Any]] = []
    used: List[tuple] = []
    slack = 10.0  # tolerate modest overruns rather than discard a good pick

    for clip in clips:
        i, j = int(clip["start_cue"]), int(clip["end_cue"])
        if not (0 <= i <= j < len(cues)):
            logger.warning("clip %r: cue range out of bounds — dropped", clip.get("title"))
            continue
        start_s = cues[i]["start_ms"] / 1000
        end_s = cues[j]["end_ms"] / 1000
        dur = end_s - start_s
        if dur < min_clip_s - slack or dur > max_clip_s + slack:
            logger.warning("clip %r: duration %.1fs outside %.0f-%.0fs — dropped",
                           clip.get("title"), dur, min_clip_s, max_clip_s)
            continue
        if any(start_s < e and end_s > s for s, e in used):
            logger.warning("clip %r: overlaps an earlier pick — dropped", clip.get("title"))
            continue
        used.append((start_s, end_s))
        out.append({
            "title": str(clip["title"])[:80],
            "hook_line": str(clip["hook_line"]),
            "keywords": [str(k) for k in clip.get("keywords", [])][:5],
            "virality": {k: max(0, min(99, int(clip["virality"][k])))
                         for k in ("hook", "flow", "value", "trend", "overall")},
            "reason": str(clip.get("reason", "")),
            "start_cue": i,
            "end_cue": j,
            "start_s": round(start_s, 3),
            "end_s": round(end_s, 3),
            "duration_s": round(dur, 3),
        })
    return out
