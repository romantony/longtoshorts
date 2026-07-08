"""Local AI-clipping driver: SRT -> Claude highlight selection -> render shorts.

The OpusClip-style flow, end to end, against the cached artifacts in ./out/:

    1. Parse the full-video SRT (cached out/transcript.srt).
    2. shorts.highlights: Claude picks scored, sentence-aligned clips with a
       title + hook line each; prints them next to the blind duration split.
    3. Per clip: slice + rebase captions (exact — no keyframe correction
       needed), build karaoke ASS with the hook overlay, frame-accurate
       CROP_FILL encode straight from the master, BGM mix.

Outputs: out/highlights.json, out/clipN_final.mp4 (N = virality rank).

Reads ANTHROPIC_API_KEY from the environment or from ./.env (gitignored).

Usage:
    .venv/bin/python local_highlights.py [--select-only] [max_clips]
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "out"

MASTER_URL = "https://cdn-v2.ai-storystudio.com/projects/js74zhgbnaatqj1fajqgwb22xh8a2gfj/e2e/concatenated/video.mp4"
SRT_URL = "https://pub-bce4924e66d944668be30268ccf4492c.r2.dev/storystudio/txt/20260707152325_a97cdf3c-6ff5-48b3-8a76-cf516da60a1c-u1_transcript.srt"
BGM_VOLUME = 0.18

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("local-highlights")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def slice_cues(cues, start_s, end_s):
    """Cues overlapping [start_s, end_s], rebased so the window starts at 0."""
    start_ms, end_ms = int(start_s * 1000), int(end_s * 1000)
    out = []
    for cue in cues:
        if cue["end_ms"] <= start_ms or cue["start_ms"] >= end_ms:
            continue
        out.append({
            "start_ms": max(cue["start_ms"], start_ms) - start_ms,
            "end_ms": min(cue["end_ms"], end_ms) - start_ms,
            "text": cue["text"],
        })
    return out


def main() -> int:
    load_dotenv(ROOT / ".env")

    select_only = "--select-only" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    max_clips = int(args[0]) if args else 3

    import requests

    from shorts import captions as cap_mod
    from shorts import highlights as hl_mod
    from shorts import probe as probe_mod
    from shorts import render as render_mod

    OUT_DIR.mkdir(exist_ok=True)

    # 1. Artifacts --------------------------------------------------------------
    srt_path = OUT_DIR / "transcript.srt"
    if not srt_path.exists():
        srt_path.write_text(requests.get(SRT_URL, timeout=60).text, encoding="utf-8")
    cues = cap_mod.parse_srt(srt_path.read_text(encoding="utf-8"))
    logger.info("parsed %d cues", len(cues))

    master = OUT_DIR / "master.mp4"
    if not select_only and not master.exists():
        probe_mod.download(MASTER_URL, master)

    # 2. AI selection ------------------------------------------------------------
    t0 = time.monotonic()
    segments = hl_mod.select_highlights(
        cues, max_clips=max_clips,
        project_title="NASA Artemis: Return to the Moon",
    )
    logger.info("selection took %.1fs", time.monotonic() - t0)

    (OUT_DIR / "highlights.json").write_text(json.dumps(segments, indent=2), encoding="utf-8")

    total_s = cues[-1]["end_ms"] / 1000
    print("\n=== Blind duration split (old) ===")
    for i in range(4):
        print(f"  part {i + 1}: {total_s / 4 * i:7.2f}s - {total_s / 4 * (i + 1):7.2f}s  'Part {i + 1}'")

    print("\n=== AI highlights (new) — ranked by virality ===")
    for seg in segments:
        v = seg["virality"]
        print(f"  #{seg['part_number']} [{v['overall']:2d}] {seg['start_s']:7.2f}s - {seg['end_s']:7.2f}s "
              f"({seg['duration_s']:.1f}s)  {seg['title']}")
        print(f"      hook({v['hook']}) flow({v['flow']}) value({v['value']}) trend({v['trend']})")
        print(f"      HOOK: \"{seg['hook_line']}\"  keywords: {', '.join(seg['keywords'])}")
        print(f"      why: {seg['reason']}")
    print()

    if select_only:
        return 0

    # 3. Render ---------------------------------------------------------------
    bgm_path = OUT_DIR / "bgm.mp3"
    for seg in segments:
        n = seg["part_number"]
        t0 = time.monotonic()

        clip_cues = cap_mod.cues_from_words(
            [w for c in slice_cues(cues, seg["start_s"], seg["end_s"])
             for w in cap_mod._even_split_words(c)],
        )
        ass_path = OUT_DIR / f"clip{n}.ass"
        ass_path.write_text(
            cap_mod.build_ass(clip_cues, None, 1080, 1920, hook_text=seg["hook_line"]),
            encoding="utf-8",
        )

        filter_value, use_complex = render_mod.build_convert_filter(
            "16:9", "9:16", 1080, 1920, "CROP_FILL",
            render_mod.DEFAULT_FG_Y_OFFSET, ass_path,
        )
        encoded = OUT_DIR / f"clip{n}_encoded.mp4"
        logger.info("clip %d: frame-accurate encode %.2fs..%.2fs + hook + captions",
                    n, seg["start_s"], seg["end_s"])
        render_mod.encode_main(master, encoded, filter_value, use_complex,
                               start_s=seg["start_s"], end_s=seg["end_s"])

        final = OUT_DIR / f"clip{n}_final.mp4"
        if bgm_path.exists():
            render_mod.mix_bgm(encoded, bgm_path, final, BGM_VOLUME)
            encoded.unlink()
        else:
            encoded.rename(final)

        actual = probe_mod.probe_duration(final)
        logger.info("clip %d done in %.1fs -> %s (%.2fs, wanted %.2fs)",
                    n, time.monotonic() - t0, final, actual, seg["duration_s"])

    print(f"\nDone. Selection: out/highlights.json — clips: out/clip1..{len(segments)}_final.mp4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
