"""Local pipeline driver: download -> probe -> segment -> trim -> 9:16 encode.

Exercises the split/convert stages of the worker locally, with no RunPod
runtime, no R2 upload, no captions/slides/BGM. Outputs land in ./out/:

    out/source.mp4            cached download (reused on re-runs)
    out/partN_raw.mp4         stream-copy trim (original aspect)
    out/partN_<style>.mp4     1080x1920 encode (blur_fill/crop_fill/pad)

Usage:
    python3 local_split.py <video_url> [num_shorts] [render_style]
"""

import json
import logging
import sys
import time
from pathlib import Path

from shorts import probe as probe_mod
from shorts import render as render_mod
from shorts import segments as seg_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("local-split")

OUT_DIR = Path(__file__).parent / "out"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    video_url = sys.argv[1]
    num_shorts = int(sys.argv[2]) if len(sys.argv) > 2 else None
    render_style = render_mod.normalize_render_style(sys.argv[3] if len(sys.argv) > 3 else None)

    OUT_DIR.mkdir(exist_ok=True)
    source_path = OUT_DIR / "source.mp4"

    # 1. Download (cached) ----------------------------------------------------
    if source_path.exists() and source_path.stat().st_size > 0:
        logger.info("reusing cached source %s (%.1f MB)",
                    source_path, source_path.stat().st_size / 2**20)
    else:
        if not probe_mod.check_url_reachable(video_url):
            logger.error("video_url is not reachable: %s", video_url[:200])
            return 1
        logger.info("downloading %s", video_url[:120])
        t0 = time.monotonic()
        probe_mod.download(video_url, source_path)
        logger.info("download took %.1fs", time.monotonic() - t0)

    # 2. Probe -----------------------------------------------------------------
    video_info = probe_mod.probe_video(source_path)
    logger.info("source: %s", json.dumps(video_info))
    if video_info["duration"] <= 0 or video_info["width"] <= 0:
        logger.error("source video unusable: %s", video_info)
        return 1

    # 3. Segments ----------------------------------------------------------------
    segments, strategy = seg_mod.resolve_segments(None, None, num_shorts,
                                                  video_info["duration"])
    logger.info("strategy=%s segments=%s", strategy, json.dumps(segments))

    source_ar = "16:9" if video_info["width"] >= video_info["height"] else "9:16"

    # 4. Per short: trim + convert -------------------------------------------------
    results = []
    for seg in segments:
        n = seg["part_number"]
        t0 = time.monotonic()

        raw_clip = OUT_DIR / f"part{n}_raw.mp4"
        if raw_clip.exists() and raw_clip.stat().st_size > 0:
            logger.info("part %d: reusing cached trim %s", n, raw_clip)
        else:
            logger.info("part %d: trimming %.1fs..%.1fs", n, seg["start_s"], seg["end_s"])
            render_mod.trim(source_path, raw_clip, seg["start_s"], seg["end_s"])

        short_clip = OUT_DIR / f"part{n}_{render_style.lower()}.mp4"
        filter_value, use_complex = render_mod.build_convert_filter(
            source_ar, "9:16", 1080, 1920, render_style,
            render_mod.DEFAULT_FG_Y_OFFSET, ass_path=None,
        )
        logger.info("part %d: encoding 1080x1920 %s", n, render_style)
        render_mod.encode_main(raw_clip, short_clip, filter_value, use_complex)

        results.append({
            "part_number": n,
            "start_s": seg["start_s"],
            "end_s": seg["end_s"],
            "raw": str(raw_clip),
            "raw_duration_s": round(probe_mod.probe_duration(raw_clip), 2),
            "short": str(short_clip),
            "short_duration_s": round(probe_mod.probe_duration(short_clip), 2),
            "short_size_mb": round(short_clip.stat().st_size / 2**20, 1),
            "encode_time_s": round(time.monotonic() - t0, 1),
        })
        logger.info("part %d done in %.1fs", n, time.monotonic() - t0)

    print(json.dumps({"strategy": strategy, "render_style": render_style,
                      "video_info": video_info, "shorts": results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
