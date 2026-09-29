#!/usr/bin/env python3
"""Build a library from media the label already owns: sources -> vertical shots + sidecars.

    python3 bin/ingest_local.py lib/briefs/top-muse.sources.yaml

`fetch_library.py` starts from a search query and somebody else's footage. This starts
from a list of files on disk — music-video loops, generated shots, cover art, press
photos — and ends in the same place: `lib/stock/<id>/*.mp4` + `*.clip.yaml`, measured by
the *same* functions, so `meme_engine.py` cannot tell the two libraries apart.

Two things differ from a download, and both are about what owned media looks like:

**A video is usually several shots, and every one of them is wanted.** A download is
trimmed to its single best window; a four-second loop that goes frame → face → frame is
three pictures, so each shot `shot_bounds` finds becomes its own clip.

**A still has to be given motion.** A cover or a press photo is one frame. It is rendered
as a slow push toward a chosen region (`crop`, fractions of the image) so it has a real
motion curve to measure, and so a still never sits dead on a beat-cut carousel. One image
can be listed several times with different crops — a face, a mouth, a horizon.

Letterboxing is detected and removed before the 9:16 fill; otherwise a 16:9 render inside
a vertical frame is scaled up with its black bars and the subject lands in a stripe.

Requires: ffmpeg, ffprobe, numpy, Pillow, pyyaml.
"""
from __future__ import annotations

import argparse
import re
from collections import Counter
import sys
from pathlib import Path

import yaml

import rtf
from fetch_library import (LOW_FPS, LOW_W, axes, decode_gray, features, motion_curve,
                           probe_display_size, run, shot_bounds)

STILL_S = 4.0        # a still becomes this long; longer than any slot it will fill
MIN_SHOT_S = 0.8     # a shot shorter than this is a flicker, not a picture


def detect_crop(src: Path) -> tuple[int, int, int, int] | None:
    """(w, h, x, y) of the picture inside any letterbox, or None if there is none.

    The limit is 48, not cropdetect's 24: a rendered letterbox is limited-range black
    (luma 16-20 with noise), which a limit of 24 reads as picture. Per-frame reset and the
    most common answer, because one bright explosion frame should not decide the box.
    """
    err = run(["ffmpeg", "-v", "info", "-i", str(src), "-vf", "cropdetect=48:2:1",
               "-f", "null", "-"]).stderr.decode(errors="replace")
    hits = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", err)
    if not hits:
        return None
    w, h, x, y = map(int, Counter(hits).most_common(1)[0][0])
    return w, h, x, y


def fill_916(pre: str = "") -> str:
    return (f"{pre}scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,fps=30,setsar=1,format=yuv420p")


def encode(args: list[str], vf: str, out: Path) -> None:
    run(["ffmpeg", "-y", "-v", "error", *args, "-vf", vf, "-an", "-c:v", "libx264",
         "-crf", "19", "-preset", "veryfast", "-movflags", "+faststart", str(out)],
        check=False)
    if not out.exists():
        raise RuntimeError(f"encode failed: {out.name}")


def measure(out: Path) -> tuple[dict, float]:
    """Features off the *normalised* clip, so a still's push is what gets measured."""
    lh = max(2, int(round(LOW_W * 1920 / 1080 / 2)) * 2)
    frames = decode_gray(out, LOW_W, lh, LOW_FPS)
    curve = motion_curve(frames)
    dur = len(frames) / LOW_FPS
    return features(out, frames, 0.0, dur, curve), dur


def sidecar(sid: str, out: Path, src: dict, path: Path, s: float, dur: float,
            f: dict) -> dict:
    return {
        "schema": "rtf.clip/v1",
        "id": sid,
        "media": out.name,
        "title": src.get("wants", sid),
        "logline": src.get("wants", ""),
        "source_meta": {"duration_ms": int(round(dur * 1000)), "width": 1080,
                        "height": 1920, "fps": 30, "aspect": "9:16"},
        "rights": {"source": "owned", "owner": src.get("owner"),
                   "license": "owned",
                   "notes": src.get("rights_notes", "label-owned media")},
        "provenance": {"file": str(path), "kind": src["kind"],
                       "crop": src.get("crop"), "section_start_s": round(s, 3),
                       "section_dur_s": round(dur, 3)},
        "features": f,
        "axes": axes(f),
        "role": {"primary": "b_roll", "can_lead": True, "can_follow": True,
                 "min_useful_ms": 400},
        "cut_points": [{"t": 0, "kind": "in"},
                       {"t": int(round(dur * 1000)), "kind": "hard_cut"}],
        # Owned media carries no burned-in captions of somebody else's; the screener is
        # for downloads. Recorded so the engine's `usable` gate still reads true.
        "screen": {"verdict": "owned", "usable": True},
    }


def ingest_still(src: dict, path: Path, outdir: Path) -> list[tuple[str, float, dict, float]]:
    w, h = probe_display_size(path)
    cx, cy, cw, ch = src.get("crop") or [0.0, 0.0, 1.0, 1.0]
    # Crop to the region first, then push in 12% over the clip. zoompan wants a
    # frame count and an output size up front.
    n = int(STILL_S * 30)
    pre = (f"crop={int(cw * w)}:{int(ch * h)}:{int(cx * w)}:{int(cy * h)},"
           f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
           f"scale=2160:3840,"
           f"zoompan=z='1+0.12*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
           f"d={n}:s=1080x1920:fps=30,")
    out = outdir / f"{src['id']}.mp4"
    encode(["-loop", "1", "-i", str(path), "-t", f"{STILL_S}"],
           pre + "setsar=1,format=yuv420p", out)
    f, dur = measure(out)
    return [(src["id"], 0.0, f, dur)]


def ingest_video(src: dict, path: Path, outdir: Path) -> list[tuple[str, float, dict, float]]:
    w, h = probe_display_size(path)
    box = detect_crop(path)
    pre = ""
    if box and (box[0] < w - 8 or box[1] < h - 8):
        pre = f"crop={box[0]}:{box[1]}:{box[2]}:{box[3]},"
    lh = max(2, int(round(LOW_W * h / w / 2)) * 2)
    curve = motion_curve(decode_gray(path, LOW_W, lh, LOW_FPS))
    shots = [(a / LOW_FPS, (b - a) / LOW_FPS) for a, b in shot_bounds(curve)]
    shots = [(s, d) for s, d in shots if d >= MIN_SHOT_S] or [(0.0, len(curve) / LOW_FPS)]

    made = []
    for k, (s, d) in enumerate(shots, 1):
        sid = src["id"] if len(shots) == 1 else f"{src['id']}-{k}"
        out = outdir / f"{sid}.mp4"
        # Trim a frame off each end: the shot boundary sample can hold half a cut.
        s2, d2 = s + 1 / LOW_FPS, max(MIN_SHOT_S, d - 2 / LOW_FPS)
        encode(["-ss", f"{s2:.3f}", "-i", str(path), "-t", f"{d2:.3f}"],
               fill_916(pre), out)
        f, dur = measure(out)
        made.append((sid, s2, f, dur))
    return made


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sources", help="rtf.sources/v1 yaml")
    ap.add_argument("--force", action="store_true", help="re-render clips that exist")
    args = ap.parse_args()

    spec_path = Path(args.sources).resolve()
    spec = yaml.safe_load(spec_path.read_text())
    root = rtf.find_root(spec_path.parent)
    outdir = root / "lib" / "stock" / spec["id"]
    outdir.mkdir(parents=True, exist_ok=True)

    for src in spec["sources"]:
        src.setdefault("owner", spec.get("owner"))
        path = Path(src["file"]).expanduser()
        if not path.exists():
            print(f"  {src['id']:<22} MISSING {path}")
            continue
        if not args.force and any(outdir.glob(f"{src['id']}*.clip.yaml")):
            print(f"  {src['id']:<22} skip (exists)")
            continue
        try:
            fn = ingest_still if src["kind"] == "still" else ingest_video
            for sid, s, f, dur in fn(src, path, outdir):
                out = outdir / f"{sid}.mp4"
                desc = sidecar(sid, out, src, path, s, dur, f)
                (outdir / f"{sid}.clip.yaml").write_text(
                    yaml.safe_dump(desc, sort_keys=False, allow_unicode=True))
                print(f"  {sid:<22} {dur:4.1f}s  E{f['energy']:.2f} L{f['luma']:.2f} "
                      f"W{f['warmth']:.2f} I{f['intensity_raw']:.2f}")
        except Exception as exc:                                  # noqa: BLE001
            print(f"  {src['id']:<22} FAIL {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
