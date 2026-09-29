#!/usr/bin/env python3
"""Generate a clip library: brief -> first frame (image model or owned art) -> Veo -> clips.

    python3 bin/generate_library.py lib/briefs/leash-her-dogs.genbrief.yaml --dry-run
    python3 bin/generate_library.py lib/briefs/leash-her-dogs.genbrief.yaml

The third way into a library, beside `fetch_library.py` (somebody else's footage) and
`ingest_local.py` (footage the label owns). A shot here is a *picture that does not exist
yet*: a line of the song taken literally, a joke the lyric sets up. Each one is

  1. a first frame — drawn by the image model from `image`, or taken from owned art via
     `frame` (a cover, a photo) so the song's own picture can come alive;
  2. eight seconds of Veo, image-to-video, from `motion`;
  3. handed to `ingest_local` as an owned video, so it is split into shots, measured and
     sidecar'd exactly like everything else the engine reads.

Everything is content-addressed in `.cache/gen/`: the image by its prompt and model, the
video by the frame's bytes, the motion prompt and model. Re-running is free and changing
one prompt costs exactly one shot. `--dry-run` prices the misses and spends nothing.

Keys: GEMINI_API_KEY (or VEO_API_KEY) from the environment or the nearest .env, else
~/Documents/AuthCodes/gemeni.key. The Gemini API route (a plain key on a billed project)
is the one that works for Veo; see generate_hook.py for why Vertex express does not.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

import rtf
import ingest_local

HOST = "https://generativelanguage.googleapis.com/v1beta"
IMAGE_MODEL = "gemini-3.1-flash-image"
VIDEO_MODEL = "veo-3.1-fast-generate-preview"
# "Price it, date it." List prices as of 2026-09; update with pricing.yaml.
PRICES_USD = {IMAGE_MODEL: 0.07, "gemini-2.5-flash-image": 0.04,
              VIDEO_MODEL: 0.15 * 8, "veo-3.1-generate-preview": 0.40 * 8,
              "veo-3.1-lite-generate-preview": 0.05 * 8}
POLL_S, POLL_MAX = 10, 60


def api_key() -> str:
    for k in ("GEMINI_API_KEY", "VEO_API_KEY"):
        if os.environ.get(k):
            return os.environ[k]
    f = Path("~/Documents/AuthCodes/gemeni.key").expanduser()
    if f.exists():
        return f.read_text().strip()
    sys.exit("no Gemini key: set GEMINI_API_KEY")


def call(url: str, body: dict | None, key: str, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as ex:
        raise RuntimeError(f"HTTP {ex.code}: {ex.read().decode()[:700]}") from None


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


def gen_image(prompt: str, model: str, key: str, out: Path) -> None:
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"],
                                 "imageConfig": {"aspectRatio": "9:16"}}}
    resp = call(f"{HOST}/models/{model}:generateContent", body, key)
    for part in resp["candidates"][0]["content"]["parts"]:
        for k in ("inlineData", "inline_data"):
            if k in part:
                out.write_bytes(base64.b64decode(part[k]["data"]))
                return
    raise RuntimeError("image model replied with text only")


def frame_from_art(path: Path, crop: list | None, out: Path) -> None:
    """Owned art as a 9:16 first frame, optionally cropped to a region first."""
    w, h = ingest_local.probe_display_size(path)
    cx, cy, cw, ch = crop or [0.0, 0.0, 1.0, 1.0]
    ingest_local.run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vf",
                      f"crop={int(cw * w)}:{int(ch * h)}:{int(cx * w)}:{int(cy * h)},"
                      "scale=1080:1920:force_original_aspect_ratio=increase,"
                      "crop=1080:1920", "-frames:v", "1", str(out)])


def gen_video(frame: Path, prompt: str, model: str, key: str, out: Path) -> None:
    mime = "image/png" if frame.suffix == ".png" else "image/jpeg"
    body = {"instances": [{"prompt": prompt, "image": {
                "bytesBase64Encoded": base64.b64encode(frame.read_bytes()).decode(),
                "mimeType": mime}}],
            "parameters": {"aspectRatio": "9:16", "durationSeconds": 8,
                           "sampleCount": 1, "personGeneration": "allow_adult"}}
    op = call(f"{HOST}/models/{model}:predictLongRunning", body, key)
    name = op.get("name") or sys.exit(f"no operation: {json.dumps(op)[:300]}")
    for _ in range(POLL_MAX):
        time.sleep(POLL_S)
        st = call(f"{HOST}/{name}", None, key)
        if st.get("error"):
            raise RuntimeError(f"Veo failed: {json.dumps(st['error'])[:500]}")
        if st.get("done"):
            samples = (((st.get("response") or {}).get("generateVideoResponse") or {})
                       .get("generatedSamples") or [])
            if not samples:
                raise RuntimeError("done but no video (safety filter?): "
                                   + json.dumps(st.get("response", {}))[:500])
            req = urllib.request.Request(samples[0]["video"]["uri"],
                                         headers={"x-goog-api-key": key})
            with urllib.request.urlopen(req, timeout=300) as r:
                out.write_bytes(r.read())
            return
    raise RuntimeError("timed out waiting for Veo")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("brief", help="rtf.genbrief/v1 yaml")
    ap.add_argument("--dry-run", action="store_true", help="price the misses, spend nothing")
    ap.add_argument("--only", help="comma-separated shot ids")
    ap.add_argument("--frames-only", action="store_true",
                    help="draw first frames and stop — look before paying for video")
    args = ap.parse_args()

    brief_path = Path(args.brief).resolve()
    brief = yaml.safe_load(brief_path.read_text())
    root = rtf.find_root(brief_path.parent)
    cache = root / ".cache" / "gen"
    cache.mkdir(parents=True, exist_ok=True)
    outdir = root / "lib" / "stock" / brief["id"]
    outdir.mkdir(parents=True, exist_ok=True)
    img_model = brief.get("image_model", IMAGE_MODEL)
    vid_model = brief.get("video_model", VIDEO_MODEL)
    style = brief.get("style", "").strip()
    only = set(args.only.split(",")) if args.only else None
    shots = [s for s in brief["shots"] if not only or s["id"] in only]

    plan, cost = [], 0.0
    for s in shots:
        if "frame" in s:
            src = Path(s["frame"]).expanduser()
            fkey = sha({"art": str(src), "mtime": src.stat().st_mtime, "crop": s.get("crop")})
            frame = cache / f"frame-{fkey}.png"
            fcost = 0.0
        else:
            prompt = f"{s['image'].strip()}\n\n{style}" if style else s["image"].strip()
            fkey = sha({"model": img_model, "prompt": prompt})
            frame = cache / f"frame-{fkey}.png"
            fcost = 0.0 if frame.exists() else PRICES_USD.get(img_model, 0.0)
        vkey = sha({"model": vid_model, "frame": fkey, "motion": s["motion"].strip()})
        video = cache / f"veo-{vkey}.mp4"
        vcost = 0.0 if (video.exists() or args.frames_only) else PRICES_USD.get(vid_model, 0)
        cost += fcost + vcost
        plan.append((s, frame, video))
        print(f"  {s['id']:<20} frame {'cached' if frame.exists() else f'${fcost:.2f}':>7}"
              f"  video {'cached' if video.exists() else f'${vcost:.2f}':>7}")
    print(f"\n{img_model} + {vid_model} · est. ${cost:.2f}")
    if args.dry_run:
        return 0

    key = api_key()
    made = []
    for s, frame, video in plan:
        try:
            if not frame.exists():
                if "frame" in s:
                    frame_from_art(Path(s["frame"]).expanduser(), s.get("crop"), frame)
                else:
                    prompt = f"{s['image'].strip()}\n\n{style}" if style else s["image"].strip()
                    gen_image(prompt, img_model, key, frame)
                print(f"  ✓ frame {s['id']}")
            if args.frames_only:
                continue
            if not video.exists():
                print(f"  … veo {s['id']}", flush=True)
                gen_video(frame, s["motion"].strip(), vid_model, key, video)
                print(f"  ✓ video {s['id']}")
            made.append((s, video))
        except Exception as exc:                                  # noqa: BLE001
            print(f"  ✗ {s['id']}: {exc}")

    # Into the library, through the same door owned footage takes.
    for s, video in made:
        for old in outdir.glob(f"{s['id']}*"):
            old.unlink()
        src = {"id": s["id"], "kind": "video", "file": str(video), "owner": brief.get("owner"),
               "wants": s.get("wants", ""),
               "rights_notes": f"generated: {img_model if 'frame' not in s else 'owned art'}"
                               f" -> {vid_model}"}
        for sid, st, f, dur in ingest_local.ingest_video(src, video, outdir):
            desc = ingest_local.sidecar(sid, outdir / f"{sid}.mp4", src, video, st, dur, f)
            desc["rights"]["source"] = "generated"
            desc["provenance"].update({"image_model": None if "frame" in s else img_model,
                                       "video_model": vid_model,
                                       "motion": s["motion"].strip(),
                                       "image": s.get("image", "").strip() or None,
                                       "frame": s.get("frame")})
            (outdir / f"{sid}.clip.yaml").write_text(
                yaml.safe_dump(desc, sort_keys=False, allow_unicode=True))
            print(f"  {sid:<22} {dur:4.1f}s  E{f['energy']:.2f} L{f['luma']:.2f} "
                  f"I{f['intensity_raw']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
