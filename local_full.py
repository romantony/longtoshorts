"""Local full-pipeline driver: split + karaoke caption burn + BGM mix.

Uses the Step Function's existing artifacts instead of per-short transcription:
a caption-free concat video, the full-video SRT, and a BGM track. No RunPod,
no R2 — everything lands in ./out/:

    out/master.mp4              cached caption-free concat video
    out/bgm.mp3                 cached BGM
    out/partN_master_raw.mp4    stream-copy trim
    out/partN.ass               karaoke captions (rebased to clip time)
    out/partN_final.mp4         1080x1920 + captions + BGM

The full-video SRT is sliced per segment. Trims are keyframe-snapped, so the
clip's true start is derived from its measured duration (true_start =
end_s - clip_duration) before rebasing cue timestamps; otherwise captions
drift by up to a couple of seconds.

Usage:
    python3 local_full.py <video_url> <srt_url> [bgm_url] [num_shorts] [style]
"""

import json
import logging
import sys
import time
from pathlib import Path

import requests

from shorts import captions as cap_mod
from shorts import probe as probe_mod
from shorts import render as render_mod
from shorts import segments as seg_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("local-full")

OUT_DIR = Path(__file__).parent / "out"
BGM_VOLUME = 0.18


def _fetch_cached(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        logger.info("reusing cached %s (%.1f MB)", dest.name, dest.stat().st_size / 2**20)
        return dest
    logger.info("downloading %s", url[:120])
    probe_mod.download(url, dest)
    return dest


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


def regroup_karaoke(cues, words_per_group=3):
    """Full-video SRT cues -> word stream -> TikTok-style N-word groups."""
    words = []
    for cue in cues:
        words.extend(cap_mod._even_split_words(cue))
    return cap_mod.cues_from_words(words, words_per_group)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    video_url, srt_url = sys.argv[1], sys.argv[2]
    bgm_url = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != "-" else None
    num_shorts = int(sys.argv[4]) if len(sys.argv) > 4 else None
    render_style = render_mod.normalize_render_style(sys.argv[5] if len(sys.argv) > 5 else "CROP_FILL")

    OUT_DIR.mkdir(exist_ok=True)

    # 1. Fetch artifacts --------------------------------------------------------
    source_path = _fetch_cached(video_url, OUT_DIR / "master.mp4")
    srt_text = requests.get(srt_url, timeout=60).text
    all_cues = cap_mod.parse_srt(srt_text)
    logger.info("parsed %d cues from full-video SRT", len(all_cues))
    bgm_path = _fetch_cached(bgm_url, OUT_DIR / "bgm.mp3") if bgm_url else None

    # 2. Probe + segments ---------------------------------------------------------
    video_info = probe_mod.probe_video(source_path)
    logger.info("source: %s", json.dumps(video_info))
    segments, strategy = seg_mod.resolve_segments(None, None, num_shorts,
                                                  video_info["duration"])
    logger.info("strategy=%s segments=%s", strategy, json.dumps(segments))
    source_ar = "16:9" if video_info["width"] >= video_info["height"] else "9:16"

    # 3. Per short ----------------------------------------------------------------
    results = []
    for seg in segments:
        n = seg["part_number"]
        t0 = time.monotonic()

        raw_clip = OUT_DIR / f"part{n}_master_raw.mp4"
        if not (raw_clip.exists() and raw_clip.stat().st_size > 0):
            logger.info("part %d: trimming %.1fs..%.1fs", n, seg["start_s"], seg["end_s"])
            render_mod.trim(source_path, raw_clip, seg["start_s"], seg["end_s"])

        clip_duration = probe_mod.probe_duration(raw_clip)
        true_start = max(0.0, seg["end_s"] - clip_duration)
        logger.info("part %d: keyframe-snapped start %.2fs (requested %.2fs)",
                    n, true_start, seg["start_s"])

        cues = regroup_karaoke(slice_cues(all_cues, true_start, seg["end_s"]))
        ass_path = None
        if cues:
            ass_path = OUT_DIR / f"part{n}.ass"
            ass_path.write_text(cap_mod.build_ass(cues, None, 1080, 1920), encoding="utf-8")

        filter_value, use_complex = render_mod.build_convert_filter(
            source_ar, "9:16", 1080, 1920, render_style,
            render_mod.DEFAULT_FG_Y_OFFSET, ass_path,
        )
        encoded = OUT_DIR / f"part{n}_encoded.mp4"
        logger.info("part %d: encoding 1080x1920 %s captions=%s",
                    n, render_style, bool(ass_path))
        render_mod.encode_main(raw_clip, encoded, filter_value, use_complex)

        final_path = OUT_DIR / f"part{n}_final.mp4"
        if bgm_path:
            logger.info("part %d: mixing BGM at %.2f", n, BGM_VOLUME)
            render_mod.mix_bgm(encoded, bgm_path, final_path, BGM_VOLUME)
        else:
            encoded.rename(final_path)

        results.append({
            "part_number": n,
            "start_s": seg["start_s"],
            "end_s": seg["end_s"],
            "true_start_s": round(true_start, 2),
            "cues": len(cues),
            "captions_applied": bool(ass_path),
            "bgm_applied": bool(bgm_path),
            "final": str(final_path),
            "duration_s": round(probe_mod.probe_duration(final_path), 2),
            "size_mb": round(final_path.stat().st_size / 2**20, 1),
            "time_s": round(time.monotonic() - t0, 1),
        })
        logger.info("part %d done in %.1fs", n, time.monotonic() - t0)

    print(json.dumps({"strategy": strategy, "render_style": render_style,
                      "video_info": video_info, "shorts": results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
