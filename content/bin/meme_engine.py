#!/usr/bin/env python3
"""Library + measured song -> beat-cut vertical videos. No model calls, no per-video cost.

    .venv/bin/python bin/meme_engine.py --library lib/stock/nocturnal --song losing-sleep \
        --variants 4 --out videos/nocturnal

This is the cheap half of "generate once, remix infinitely" ([BUILD-SPEC §7b]). The
library is the expensive artifact; an edit off it is ffmpeg and a few seconds of CPU, so
the marginal cost of the fifth variant is not a rounding error on the first — it is zero.
That is the whole reason the engine is deterministic and seeded rather than generative.

**Congruence, mechanically.** The format's geometry says: hold the *figure*, invert the
*ground*. Here the figure is everything the engine refuses to vary within a video — one
grade, one aspect, one cut grid, hard cuts only. The ground is the four axes
`fetch_library.py` measured off the pixels (light, motion, temp, scale), and the running
order is *chosen* so those axes keep flipping. That turns "does this feel varied" into
two checks that either pass or do not: every axis gets used, and no axis leads more than
twice running.

**The order is solved, not authored.** Slots carry an intensity target that climbs and
peaks in the final third; clips are fitted to it greedily and then hill-climbed on a cost
that treats axis runs and repeats as hard constraints. Authoring in the order the ideas
arrived is what made both hand-built videos fail the escalation check at +0.29.

Requires: ffmpeg, numpy, pyyaml.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import random
import subprocess
import sys
from pathlib import Path

import yaml

import rtf

FPS = 30
W, H = 1080, 1920
# First one that exists: the engine was written on a Mac and also has to run on Linux.
FONT = next((f for f in (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/julietaula-montserrat-fonts/Montserrat-Black.otf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
) if Path(f).exists()), "DejaVu Sans")

AXES = ["light", "motion", "temp", "scale"]

# The figure: one grade for the whole video, and the same grade for every video off this
# library. Kept as a plain filter string so a colourist can read and argue with it.
LOOKS = {
    "none": "",
    "night": ("eq=contrast=1.10:brightness=0.03:saturation=1.20:gamma=1.05,"
              "colorbalance=rs=-0.03:bs=0.05:rh=0.05:bh=-0.03,"
              "vignette=PI/6,noise=alls=4:allf=t"),
    "bleach": ("eq=contrast=1.10:saturation=1.30:gamma=1.03,"
               "colorbalance=rs=0.06:bs=-0.05,vignette=PI/6"),
    "vhs": ("chromashift=cbh=3:crh=-3,eq=contrast=1.12:saturation=1.35,"
            "noise=alls=11:allf=t,vignette=PI/4"),
}

# A move per slot, as a zoompan zoom expression over `on` (this slot's output frame
# index). Video already moves; these are the cut's own gesture, and their only job is to
# stop two adjacent shots reading as one long take across the cut.
#
# It has to be zoompan and not crop: crop evaluates w/h once at configure time, so `t`
# is not in scope there and only x/y can animate — which gives a pan, never a zoom.
MOVES = {
    "none":  None,
    "push":  "1+0.10*on/{N}",
    "pull":  "1.10-0.10*on/{N}",
    "punch": "1.18-0.18*min(1,on/({N}*0.30))",
}


def esc(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’")
             .replace("%", "\\%"))


def fit_fontsize(text: str, max_px: int, cap: int) -> int:
    """Largest size that still fits inside the frame.

    drawtext does not wrap or shrink — it draws off the edge and says nothing, which is
    how an end card ends up reading "sing Sleep — Hallow You". Arial Bold averages about
    0.58em per character, which is close enough to size a one-line card from.
    """
    if not text:
        return cap
    return max(24, min(cap, int(max_px / (0.58 * len(text)))))


def match_gamma(luma: float, target: float = 0.30) -> float:
    """Per-clip exposure match, so fifteen sources read as one video.

    This is not the look — the look is one filter string applied to the whole timeline
    and never varied. This is the thing that has to happen *before* a shared look means
    anything: a 0.07-luma rain plate and a 0.45-luma highway plate graded identically
    still cut like two different videos. Solving for eq's gamma, where out = in^(1/g).
    """
    L = min(max(luma, 0.02), 0.95)
    g = math.log(L) / math.log(target)
    return round(min(1.9, max(0.75, g)), 3)


# ---------------------------------------------------------------- captions


def read_lrc(path: Path) -> list[tuple[float, str]]:
    """[(seconds, line)] from an .lrc. Lines without a timestamp are notes, not lyrics."""
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.startswith("[") or "]" not in raw:
            continue
        stamp, text = raw[1:].split("]", 1)
        try:
            m, sec = stamp.split(":")
            t = int(m) * 60 + float(sec)
        except ValueError:
            continue                      # [ar:…] style tags
        if text.strip():
            out.append((t, text.strip()))
    return sorted(out)


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_text(s: str) -> str:
    """Braces would open an override block. A newline, or a typed "\\n" from the shell,
    is a line break — how a two-line meme ("museum: no pets allowed / her:") is written."""
    return (s.replace("{", "(").replace("}", ")")
             .replace("\\n", "\\N").replace("\n", "\\N"))


def write_ass(path: Path, lyrics: list[tuple[float, str]], audio_start: float,
              end_t: float, caption: str | None, tag: str | None = None) -> None:
    """Burn-in captions: a meme line on top, the sung lyric at the bottom.

    Both stop where the end card starts — the card is the release and should be alone.
    Placement keeps inside TikTok/Reels chrome: nothing in the top ~220px (status and
    tabs) and nothing below ~1420px (caption, handle, sound), and a right margin clear of
    the like/comment rail.

    Lyric timing is the .lrc's line starts, shifted by where the audio is cut from. A line
    ends at the next line's start (or 4.5s, whichever is sooner): an .lrc has no end
    times, and a line left up through an instrumental reads as a stuck subtitle.
    """
    font = "Montserrat"
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font} ExtraBold,70,&H00111111,&H00111111,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,18,0,8,90,90,250,1
Style: Tag,{font} Bold,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,1,60,60,470,1
Style: Lyric,{font} Black,86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,80,140,540,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    if caption:
        ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(end_t)},Caption,,0,0,0,,"
                  f"{ass_text(caption)}")
    if tag:
        ev.append(f"Dialogue: 2,{ass_time(0)},{ass_time(end_t)},Tag,,0,0,0,,"
                  f"{ass_text(tag)}")
    for k, (t, line) in enumerate(lyrics):
        a = t - audio_start
        nxt = lyrics[k + 1][0] - audio_start if k + 1 < len(lyrics) else a + 4.5
        b = min(nxt, a + 4.5, end_t)
        if b <= 0.05 or a >= end_t:
            continue
        ev.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Lyric,,0,0,0,,"
                  f"{ass_text(line)}")
    path.write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- library


def load_library(d: Path) -> list[dict]:
    clips = []
    for side in sorted(d.glob("*.clip.yaml")):
        c = yaml.safe_load(side.read_text())
        media = d / c["media"]
        if not media.exists():
            continue
        c["_path"] = media
        c["_dur"] = c["source_meta"]["duration_ms"] / 1000.0
        clips.append(c)
    if not clips:
        sys.exit(f"no clips with sidecars in {d}")
    return clips


def rank_intensity(clips: list[dict]) -> None:
    """1-5 by rank, not by absolute value.

    An absolute threshold would call a whole library of night footage 'low intensity' and
    then have nothing to escalate to. Intensity is only ever meaningful relative to the
    other pictures in the same video.
    """
    order = sorted(clips, key=lambda c: c["features"]["intensity_raw"])
    n = len(order)
    for i, c in enumerate(order):
        c["_intensity"] = 1 + round(4 * i / max(1, n - 1))


def normalize_axes(clips: list[dict]) -> None:
    """Score each axis against the library's own spread, not against an absolute 0.5.

    Raw axis values are not comparable to each other: across a night library `light` and
    `motion` use the full 0-1 range while `temp` sits inside 0.43-0.69. Ranking by raw
    distance from centre therefore let light and motion win every single time and `temp`
    never led a slot — the axis was declared, measured, and then structurally unusable.
    Standardising per axis makes "most extreme" mean the same thing on all four.
    """
    for a in AXES:
        vals = [c["axes"][a]["v"] for c in clips]
        mu = sum(vals) / len(vals)
        sd = (sum((v - mu) ** 2 for v in vals) / max(1, len(vals) - 1)) ** 0.5 or 1e-6
        for c in clips:
            c.setdefault("_z", {})[a] = (c["axes"][a]["v"] - mu) / sd


def primary_axis(c: dict) -> str:
    """The axis this clip is most extreme on — the one it 'leads' with."""
    if "_z" in c:
        return max(AXES, key=lambda a: abs(c["_z"][a]))
    return max(AXES, key=lambda a: abs(c["axes"][a]["v"] - 0.5))


# ---------------------------------------------------------------- the grid


def build_slots(song: dict, build_bars: int, build_cut_beats: float,
                drop_beats: float, end_beats: float) -> list[dict]:
    """Slot durations in frames, from absolute positions on the beat grid.

    Positions are absolute and durations are their differences, so rounding never
    accumulates: the last cut is still on the beat. (Same reason as render_edit.py.)
    """
    beat_ms = 60000.0 / float(song["measured"]["bpm"])
    slots, pos = [], 0.0

    n_build = int(round(build_bars * 4 / build_cut_beats))
    for i in range(n_build):
        slots.append({"beats": build_cut_beats, "at": pos, "section": "build"})
        pos += build_cut_beats

    drop_at = pos
    remaining = drop_beats - end_beats
    i = 0
    while remaining > 1e-6:
        b = min(1.0, remaining)
        slots.append({"beats": b, "at": pos, "section": "drop"})
        pos += b
        remaining -= b
        i += 1
    slots.append({"beats": end_beats, "at": pos, "section": "end"})
    pos += end_beats

    edges = [round(s["at"] * beat_ms / 1000.0 * FPS) for s in slots] + \
            [round(pos * beat_ms / 1000.0 * FPS)]
    for s, a, b in zip(slots, edges, edges[1:]):
        s["frames"] = max(2, b - a)
        s["start_f"] = a
    return slots, beat_ms, drop_at


def target_curve(slots: list[dict]) -> list[float]:
    """Intensity the running order should be hitting at each slot.

    Climbs through the build, climbs harder after the drop, peaks at ~85% of the drop
    section, and steps down for the end card — which is the release, not the climax.
    """
    drop_idx = [i for i, s in enumerate(slots) if s["section"] == "drop"]
    out = []
    for i, s in enumerate(slots):
        if s["section"] == "build":
            n = max(1, sum(1 for x in slots if x["section"] == "build"))
            out.append(1.6 + 1.4 * (i / n))
        elif s["section"] == "drop":
            u = drop_idx.index(i) / max(1, len(drop_idx) - 1)
            out.append(3.0 + 2.0 * (u / 0.85) if u <= 0.85
                       else 5.0 - 1.2 * ((u - 0.85) / 0.15))
        else:
            out.append(2.6)
    return out


# ---------------------------------------------------------------- ordering


def cost(order: list[dict], target: list[float], max_run: int) -> float:
    """Lower is better. Hard constraints are priced high enough to always dominate."""
    c = 0.0
    for i, (clip, t) in enumerate(zip(order, target)):
        c += (clip["_intensity"] - t) ** 2

    for a, b in zip(order, order[1:]):
        if a["id"] == b["id"]:
            c += 400.0                       # the same shot twice reads as a freeze

    run, prev = 1, None
    for clip in order:
        ax = primary_axis(clip)
        run = run + 1 if ax == prev else 1
        prev = ax
        if run > max_run:
            c += 250.0                       # one axis leading too long is a mood board

    used = {primary_axis(c_) for c_ in order}
    c += 150.0 * len(set(AXES) - used)       # a declared axis nobody hits isn't declared

    n3 = max(1, len(order) // 3)
    first = sum(x["_intensity"] for x in order[:n3]) / n3
    last = sum(x["_intensity"] for x in order[-n3:]) / n3
    c += 120.0 * max(0.0, 0.5 - (last - first))   # it has to actually climb

    if not peaks_late(order):
        c += 120.0                           # deflating early wastes the drop
    return c


def peaks_late(order: list[dict]) -> bool:
    """Does the library's top intensity actually occur in the final third?

    Ties are the normal case — a 15-clip library has only five distinct levels — so this
    asks whether the maximum *occurs* late, not whether its first occurrence is late.
    The stricter reading fails a sequence that holds its peak across the whole back half,
    which is the shape we are trying to produce.
    """
    top = max(c["_intensity"] for c in order)
    cut = int(len(order) * 2 / 3)
    return any(c["_intensity"] == top for c in order[cut:])


def solve_order(clips: list[dict], slots: list[dict], seed: int,
                max_run: int) -> list[dict]:
    """Greedy fit to the intensity curve, then hill-climb on pairwise swaps.

    Running order is free to change — same clips, same library, same cost — so there is
    no reason to accept the order the ideas arrived in.
    """
    rng = random.Random(seed)
    n = len(slots)

    # Variety has to come from the inputs, not from the search. Re-seeding the hill-climb
    # alone produced three near-identical edits: with five intensity levels over fifteen
    # clips the target curve nearly determines each slot, so every seed walked to the same
    # optimum. Each variant therefore casts from a different subset and chases a slightly
    # different curve — genuinely different videos that still pass the same checks.
    cast = clips[:]
    rng.shuffle(cast)
    keep = max(10, int(round(len(clips) * 0.8)))
    cast = cast[:keep]

    # Sampling must not drop the only clip that leads an axis, or the variant fails the
    # coverage check for a reason no reordering can fix.
    for a in AXES:
        if not any(primary_axis(c) == a for c in cast):
            champ = max((c for c in clips if primary_axis(c) == a),
                        key=lambda c: abs(c["_z"][a]), default=None)
            if champ is not None:
                cast.append(champ)

    target = [t + rng.uniform(-0.35, 0.35) for t in target_curve(slots)]

    pool: list[dict] = []
    while len(pool) < n:
        shuffled = cast[:]
        rng.shuffle(shuffled)
        pool.extend(shuffled)

    order: list[dict] = []
    avail = pool[:]
    for i in range(n):
        best, best_k = None, None
        for k, c in enumerate(avail):
            if order and c["id"] == order[-1]["id"]:
                continue
            d = abs(c["_intensity"] - target[i]) + rng.random() * 0.35
            if best is None or d < best:
                best, best_k = d, k
        if best_k is None:
            best_k = 0
        order.append(avail.pop(best_k))

    cur = cost(order, target, max_run)
    for _ in range(6000):
        i, j = rng.randrange(n), rng.randrange(n)
        if i == j:
            continue
        order[i], order[j] = order[j], order[i]
        new = cost(order, target, max_run)
        if new <= cur:
            cur = new
        else:
            order[i], order[j] = order[j], order[i]
    return order


def assign_moves(order: list[dict], slots: list[dict], seed: int) -> list[str]:
    """No two adjacent slots get the same gesture — that is the whole rule."""
    rng = random.Random(seed + 77)
    kinds = ["push", "punch", "pull", "none"]
    out, prev = [], None
    for s in slots:
        pick = [k for k in kinds if k != prev]
        if s["section"] == "build":
            pick = [k for k in pick if k != "punch"]   # save the snap for the drop
        m = rng.choice(pick)
        out.append(m)
        prev = m
    return out


def pick_windows(order: list[dict], slots: list[dict], seed: int) -> list[float]:
    """Where inside each clip this slot takes its frames.

    A clip used three times must not show the same 480ms three times, so uses walk the
    clip by an irrational-ish stride and wrap.
    """
    rng = random.Random(seed + 991)
    seen: dict[str, int] = {}
    starts = []
    for clip, s in zip(order, slots):
        k = seen.get(clip["id"], 0)
        seen[clip["id"]] = k + 1
        need = s["frames"] / FPS
        room = max(0.0, clip["_dur"] - need - 0.05)
        starts.append(round((k * 0.37 + rng.random() * 0.13) % 1.0 * room, 3) if room > 0 else 0.0)
    return starts


# ---------------------------------------------------------------- render


def build_filter(order, slots, moves, starts, look, total_f, drop_f, cta, cta_sub,
                 exposure=0.30, ass=None):
    parts, labels = [], []
    for i, (clip, s, mv, st) in enumerate(zip(order, slots, moves, starts)):
        d = s["frames"] / FPS
        chain = (f"[{i}:v]trim=start={st:.3f}:duration={d + 0.4:.3f},"
                 f"setpts=PTS-STARTPTS,fps={FPS},"
                 f"tpad=stop_mode=clone:stop_duration=2")
        g = match_gamma(clip["features"]["luma"], exposure)
        if abs(g - 1.0) > 0.02:
            chain += f",eq=gamma={g}"
        z = MOVES[mv]
        if z:
            zz = z.format(N=max(1, s["frames"]))
            chain += (f",zoompan=z='{zz}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                      f"d=1:s={W}x{H}:fps={FPS}")
        # The frame-count trim comes last on purpose: it is the only thing guaranteeing
        # the slot is exactly as long as the grid says, whatever zoompan did to timing.
        chain += (f",trim=end_frame={s['frames']},setpts=PTS-STARTPTS,"
                  f"setsar=1[v{i}]")
        parts.append(chain)
        labels.append(f"[v{i}]")

    parts.append("".join(labels) + f"concat=n={len(order)}:v=1:a=0[cat]")

    stream = "[cat]"
    if LOOKS.get(look):
        parts.append(f"{stream}{LOOKS[look]}[graded]")
        stream = "[graded]"

    # Two hard white frames on the drop. Not a dissolve — a dissolve smears across the
    # beat, and the beat is the entire point.
    flash_t = drop_f / FPS
    parts.append(f"color=c=white:s={W}x{H}:r={FPS}:d={total_f / FPS + 1}[wf]")
    parts.append(f"{stream}[wf]overlay=enable='between(t,{flash_t:.3f},"
                 f"{flash_t + 2.0 / FPS:.3f})'[flash]")
    stream = "[flash]"

    if ass:
        parts.append(f"{stream}ass='{ass}'[subbed]")
        stream = "[subbed]"

    if cta:
        end_t = (total_f - slots[-1]["frames"]) / FPS
        en = f"enable='gte(t,{end_t:.3f})'"
        fs1 = fit_fontsize(cta, W - 120, 96)
        fs2 = fit_fontsize(cta_sub, W - 200, 46)
        parts.append(
            f"{stream}drawbox=x=0:y=0:w=iw:h=ih:color=black@0.55:t=fill:{en},"
            f"drawtext=fontfile={FONT}:text='{esc(cta)}':fontsize={fs1}:fontcolor=white:"
            f"x=(w-text_w)/2:y=h*0.42:{en},"
            f"drawtext=fontfile={FONT}:text='{esc(cta_sub)}':fontsize={fs2}:"
            f"fontcolor=white@0.82:x=(w-text_w)/2:y=h*0.50:{en}[vout]")
        stream = "[vout]"
    else:
        parts.append(f"{stream}null[vout]")
        stream = "[vout]"
    return ";".join(parts), stream


def render(order, slots, moves, starts, song_path, audio_start_s, total_f,
           drop_f, look, cta, cta_sub, out: Path, exposure=0.30, ass=None) -> int:
    inputs = []
    for clip in order:
        inputs += ["-i", str(clip["_path"])]
    ai = len(order)
    inputs += ["-ss", f"{audio_start_s:.3f}", "-i", str(song_path)]

    fc, vlab = build_filter(order, slots, moves, starts, look, total_f, drop_f,
                            cta, cta_sub, exposure, ass)
    total_s = total_f / FPS
    fc += (f";[{ai}:a]atrim=duration={total_s:.3f},asetpts=N/SR/TB,"
           f"afade=t=in:st=0:d=0.04,"
           f"afade=t=out:st={max(0.0, total_s - 0.35):.3f}:d=0.35[aout]")

    cmd = ["ffmpeg", "-y", "-v", "error", *inputs,
           "-filter_complex", fc, "-map", vlab, "-map", "[aout]",
           "-frames:v", str(total_f),
           # The overlay/concat graph hands out a 1/1000000 timebase; x264 then sizes the
           # level for a million-fps stream (6.2), which phones, QuickTime and browsers
           # refuse to open. Pin the rate and level, and carry nothing but video + audio.
           "-r", str(FPS), "-video_track_timescale", str(FPS * 1000),
           "-dn", "-sn", "-map_chapters", "-1", "-map_metadata", "-1",
           "-c:v", "libx264", "-profile:v", "high", "-level:v", "4.2",
           "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    return subprocess.call(cmd)


# ---------------------------------------------------------------- report


def report(order: list[dict], slots: list[dict], moves: list[str]) -> list[str]:
    lines = []
    n3 = max(1, len(order) // 3)
    first = sum(c["_intensity"] for c in order[:n3]) / n3
    last = sum(c["_intensity"] for c in order[-n3:]) / n3
    top = max(c["_intensity"] for c in order)
    peak = max(i for i, c in enumerate(order) if c["_intensity"] == top)

    run, mx, prev = 1, 1, None
    for c in order:
        ax = primary_axis(c)
        run = run + 1 if ax == prev else 1
        prev, mx = ax, max(mx, run)

    used = {primary_axis(c) for c in order}
    lines.append(f"  axes hit     {len(used)}/{len(AXES)}  ({', '.join(sorted(used))})")
    lines.append(f"  max axis run {mx}")
    lines.append(f"  lift         {last - first:+.2f}  (first third {first:.2f} -> "
                 f"last third {last:.2f}, floor +0.50)")
    lines.append(f"  peak at      slot {peak + 1}/{len(order)} "
                 f"({'final third OK' if peaks_late(order) else 'TOO EARLY'})")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", required=True)
    ap.add_argument("--song", default="losing-sleep")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--variants", type=int, default=3)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--look", default="night", choices=sorted(LOOKS))
    ap.add_argument("--build-bars", type=float, default=4)
    ap.add_argument("--build-cut-beats", type=float, default=2)
    ap.add_argument("--drop-beats", type=float, default=None,
                    help="default: the song's tuning.carousel_beats")
    ap.add_argument("--end-beats", type=float, default=2)
    ap.add_argument("--max-axis-run", type=int, default=2)
    ap.add_argument("--cta", default=None, help="end card headline; '' to disable")
    ap.add_argument("--cta-sub", default="stream now — link in bio")
    ap.add_argument("--exposure", type=float, default=0.30,
                    help="luma every clip is matched toward; 0.30 suits night footage, "
                         "a bright library wants ~0.5")
    ap.add_argument("--lrc", default=None,
                    help="burn in lyrics from this .lrc ('song' = the song's lyrics_timed)")
    ap.add_argument("--tag", default=None,
                    help="small sound credit, bottom left ('♪ Title — Artist'); for a "
                         "meme that is not an ad, use this with --cta ''")
    ap.add_argument("--caption", action="append", default=[],
                    help="meme line on top; repeat to give each variant its own")
    ap.add_argument("--check", action="store_true", help="print the plan, render nothing")
    args = ap.parse_args()

    lib = Path(args.library)
    root = rtf.find_root(lib.resolve() if lib.exists() else Path.cwd())
    song_file = root / "lib" / "songs" / f"{args.song}.song.yaml"
    if not song_file.is_file():
        sys.exit(f"no song at {song_file}")
    song = yaml.safe_load(song_file.read_text())
    song_path = (song_file.parent / song["file"]).resolve()
    if not song_path.exists():
        sys.exit(f"song audio missing: {song_path}")

    clips = load_library(lib)
    rank_intensity(clips)
    normalize_axes(clips)

    drop_beats = args.drop_beats or song.get("tuning", {}).get("carousel_beats", 23)
    slots, beat_ms, drop_at = build_slots(song, args.build_bars, args.build_cut_beats,
                                          float(drop_beats), args.end_beats)
    total_f = slots[-1]["start_f"] + slots[-1]["frames"]
    drop_f = next(s["start_f"] for s in slots if s["section"] == "drop")
    audio_start = (song["measured"]["drop_ms"] - drop_at * beat_ms) / 1000.0

    cta = args.cta if args.cta is not None else f"{song['title']} — {song['artist']}"

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    lyrics = []
    if args.lrc:
        lrc = Path(song["lyrics_timed"] if args.lrc == "song" else args.lrc)
        lyrics = read_lrc(lrc if lrc.is_absolute() else song_file.parent / lrc)
    # With an end card the captions give way to it; without one they run to the end.
    end_t = (total_f - slots[-1]["frames"]) / FPS if cta else total_f / FPS

    print(f"library {lib}  {len(clips)} clips")
    print(f"song    {song['title']} @ {song['measured']['bpm']} bpm, "
          f"beat {beat_ms:.0f}ms, drop {song['measured']['drop_ms']}ms")
    print(f"grid    {len(slots)} slots, {total_f} frames = {total_f / FPS:.2f}s  "
          f"(drop at {drop_f / FPS:.2f}s), audio from {audio_start:.2f}s\n")

    for v in range(args.variants):
        seed = args.seed + v * 1013
        order = solve_order(clips, slots, seed, args.max_axis_run)
        moves = assign_moves(order, slots, seed)
        starts = pick_windows(order, slots, seed)

        print(f"variant {v + 1}  seed {seed}")
        for line in report(order, slots, moves):
            print(line)

        plan = {
            "schema": "rtf.memeplan/v1",
            "song": args.song, "library": str(lib), "seed": seed, "look": args.look,
            "fps": FPS, "total_frames": total_f, "drop_frame": drop_f,
            "audio_start_s": round(audio_start, 3),
            "slots": [
                {"i": i, "section": s["section"], "clip": c["id"],
                 "frames": s["frames"], "start_s": st, "move": m,
                 "intensity": c["_intensity"], "axis": primary_axis(c)}
                for i, (c, s, m, st) in enumerate(zip(order, slots, moves, starts))
            ],
        }
        out = outdir / f"{outdir.name}-v{v + 1}.mp4"
        (outdir / f"{outdir.name}-v{v + 1}.plan.yaml").write_text(
            yaml.safe_dump(plan, sort_keys=False))

        if args.check:
            print(f"  (check) would write {out}\n")
            continue

        ass = None
        caption = args.caption[v % len(args.caption)] if args.caption else None
        if lyrics or caption or args.tag:
            ass = outdir / f"{outdir.name}-v{v + 1}.ass"
            write_ass(ass, lyrics, audio_start, end_t, caption, args.tag)
            plan["caption"] = caption
            (outdir / f"{outdir.name}-v{v + 1}.plan.yaml").write_text(
                yaml.safe_dump(plan, sort_keys=False, allow_unicode=True))

        rc = render(order, slots, moves, starts, song_path, audio_start, total_f,
                    drop_f, args.look, cta, args.cta_sub, out, args.exposure,
                    ass.resolve() if ass else None)
        print(f"  -> {out}" if rc == 0 else f"  ffmpeg failed rc={rc}", "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
