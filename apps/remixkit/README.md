---
title: "RemixKit — the artist console"
subtitle: "Register an artist, build their identity once, attach songs, generate the media a release needs in whichever format it needs, and verify what came out."
status: "LIVE — deployed on AWS with real generation, real B2, and real auth. Also runs end-to-end with zero credentials."
date: "2026-07-27"
---

> **On the spec citations below.** This document and the code cite `PRODUCT.md`,
> `BUILD-SPEC`, `MEMORY-SPEC` and `PIPELINE-SPEC` by section. All four are **void as
> plan** — see [`docs/SCOPE-RESET.md`](../docs/SCOPE-RESET.md). The citations are kept
> because they record *why this code looks the way it does*; none of them describes what
> to build next. RemixKit is now a subproject of the platform, not the product.

## Run it

No credentials, no bucket, no API keys, no database.

```bash
cd app
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn remixkit.main:app --reload
```

Everything the app needs is read from the repo-root `.env` — including `RK_SSM_PATH`, which is how
real credentials arrive. Nothing needs exporting first: `source .env` would not work
anyway, because a plain `KEY=value` line sets a shell variable rather than an exported
one, so a child process never sees it.

Open <http://localhost:8000/console>. Register an artist, record likeness consent, save
an identity, attach a song, set its hook window, generate a kit. Real videos appear.

<http://localhost:8000> is the public landing page — the B2B site, served by this same
app, and the only page that renders without a session. See [Two front doors](#two-front-doors).

`ffmpeg` is the one non-Python dependency (`brew install ffmpeg`). Without it the mock
generator reports the modality unavailable rather than producing something misleading.

```bash
.venv/bin/python -m pytest        # 67 tests, ~2s
```

---

## What "mock" does and does not mean

This matters more than it usually would, because a demo that looks live while it is
mocked misleads by accident.

**Real:** the Genblaze `Pipeline`, the `ObjectStorageSink`, the `HIERARCHICAL` key
layout, content hashing, the manifest, `manifest.verify()`, the cost ledger, and the
embed-on-delivery path. **Synthetic:** the pixels, which ffmpeg generates, and the unit
costs, which come from a table.

The consequence worth stating: *the manifest verified on a laptop is produced by the
same code that produces the one in B2.* Switching to real providers changes
`RK_GENERATOR_BACKEND` and nothing else.

Every page carries a banner naming which backends are live, and `/healthz` returns the
same thing as JSON.

---

## Shape

Hexagonal, so that "componentized, and we add auth later" is a configuration change
rather than a refactor.

```
remixkit/
  domain/models.py     Artist · Identity · Song · SongSection · LyricLine · Kit · Asset
                       — pure, no I/O
  ports/               Protocols: Storage · DocumentRepository · Generator · JobQueue
                       · AudioAnalyzer · Transcriber
  adapters/            storage_local · storage_b2 · repo_documents
                       generator_genblaze · providers (mock | live) · mock_media
                       queue_inline · queue_sqs
                       audio_numpy · audio_unavailable
                       lyrics_elevenlabs · lyrics_unavailable
  services/            artists · identities · songs · briefs · kits · delivery · verify
                       accounts  ← sign-in codes and sessions
                       analysis  ← measure a master  ·  recommendations ← which hook
                       transcription ← read the words off it
  auth/                provider (Protocol) · anonymous · otp
  api/v1.py            JSON API
  ui/                  Jinja + htmx components, plus pages/landing.html — the site
  deps.py              the composition root — the only file that names an adapter
```

Seven axes, seven environment variables:

| | dev default | production |
|---|---|---|
| `RK_STORAGE_BACKEND` | `local` | `b2` |
| `RK_GENERATOR_BACKEND` | `mock` | `genblaze` |
| `RK_QUEUE_BACKEND` | `inline` | `sqs` |
| `RK_MAIL_BACKEND` | `console` | `resend` |
| `RK_AUTH_BACKEND` | `none` | `otp` |
| `RK_ANALYSIS_BACKEND` | `auto` | `auto` |
| `RK_TRANSCRIPTION_BACKEND` | `auto` | `auto` |

`analysis` and `transcription` are the two axes with no mock — the only two where a
zero-credential fake would produce output nothing downstream could tell from the real
thing. See below.

---

## Songs have as many hooks as they have

A song used to carry one `hook` window, which said every track has exactly one loopable
moment. `Song.sections` replaces that: a list of named stretches — intro, verse, chorus,
drop, breakdown — of which any tagged hook, chorus or drop can be cut to. A kit may be cut
to several, and each loop is the length of *its own* window. The videos are dealt
round-robin across the chosen hooks rather than multiplied by them, so ticking a second
hook does not double the invoice without the video count being changed too.

One window is still the *primary* hook, mirrored onto `song.hook`, and it is what a kit
cuts to when its brief names no section. Setting it on the old form moves the section, so
the two surfaces cannot disagree about what will render.

**Every section records where its window came from.** `measured` carries the analyser's
method line; `manual` means somebody drew it. Moving a measured window makes it manual and
keeps the old method as history — a window a person dragged still reading "measured" would
be the provenance rule defeated by the edit form.

## Measuring the master

Upload an MP3 or WAV on the song page and the structure is measured from the audio:
tempo, the drop, the riser, the plateau, and the sections. The bytes go browser→bucket
(§2b rule 2) and the measurement runs as a queued job (§2b rule 3), inline on a laptop and
on Batch in the deployment — `python worker.py --song-id sng_…`.

The methods are not new. `content/RHYTHM-STUDY.md` measured one song to death and recorded
which approaches work on this material and which do not, and `adapters/audio_numpy.py` is
a port of the two tools that study validated:

| | how, and why not the standard move |
|---|---|
| tempo | joint tempo+phase comb fit. Autocorrelation alone gives a good period and a phase that wanders. |
| drop | largest positive step in sub-200 Hz energy. Foote checkerboard novelty ranked the study's measured drop 8th–79th, never 1st — a drop is a *re-entry*, which a checkerboard scores low. |
| bar line | the residue mod 4 of the largest arrangement changes. On four-on-the-floor an accent comb has no signal: four phases within 5–9%, disagreeing about the winner. |
| section end | the first bar whose kick falls past 3× the typical bar-to-bar movement. A mastered drop is limited, so RMS barely moves at a boundary while the low band falls off a cliff. |

`tests/test_audio_numpy.py` runs it against the study's own master and asserts the study's
own published numbers — 125 BPM, the drop at 124784 ms, an 8-beat riser, a 28-beat
plateau, 5 of 6 changes sharing a residue. A port that does not reproduce the original's
findings on the original's material is a different algorithm wearing its docstrings.

**There is no mock analyser, and that asymmetry is deliberate.** Every other adapter has a
zero-credential version that does the real thing badly; a mocked measurement is a float
that nothing downstream — not the console, not a kit brief, not a person reading the song
six months later — could distinguish from a measured one. So a process without numpy
refuses and names what is missing, on the song page and in the `/healthz` warnings.

```bash
pip install -e '.[audio]'     # numpy; ffmpeg on PATH for anything but a WAV
```

## Reading the words off it

The other question the same master answers. Analysis says *where the hooks are*;
transcription says *what is being sung in them* — and it runs the same way, as a queued job
over the stored bytes: `python worker.py --transcribe-song-id sng_…`, inline on a laptop,
Batch in the deployment.

ElevenLabs Scribe, direct rather than through Genblaze: `genblaze-elevenlabs` generates
speech and this is the other direction, with no generator step to hang it on. The key is
the one `adapters/providers.py` already reads for TTS, so a keyed deployment gains this
without gaining a vendor.

**A lyric is a guess at language, and the console never pretends otherwise.** The provider
returns words; a lyric is lines, so `adapters/lyrics_elevenlabs.py` groups them on three
stated thresholds — a 900 ms gap, 42 characters, sentence-final punctuation — and every one
of them is in the `method` line stored on the song. Each line carries the geometric mean of
its words' probabilities, and the ones below 0.6 are flagged **check by ear** rather than
hidden: they are the list of lines to play back, and burying them would make a transcript
look finished when the whole point of the number is that it is not.

**Every line is editable, and editing one records that a person did.** Same rule as a
section window: a transcribed line somebody rewrote becomes `manual`, keeps the old method
as history, and *loses the confidence* — a machine's doubt attached to a sentence a human
decided would be the provenance rule defeated by the edit form. Fixing a chorus the model
heard as gibberish is one textarea, not eight round trips: the bulk edit keeps the windows
already known by pairing lines positionally, leaves untouched lines measured, and says so
when the line count moves and the pairing becomes a guess.

Re-transcribing **replaces** the lyric — a lyric is one continuous reading, so interleaving
old corrections with new lines produces a transcript that is neither. A song with
hand-corrected lines therefore refuses the second run and says how many would be lost;
`force` is the caller saying yes anyway, and the console asks before sending it.

**There is no mock transcriber**, for a sharper version of the analyser's argument. A
fabricated measurement is a wrong number; a fabricated lyric is *words attributed to an
artist* — stored as the song's own, offered to the generate form as hook lines, and burned
into a clip published under the label's name. A lyric can still be typed by hand on a
process with no credentials, which is what a label with the sheet already wants anyway.

```bash
pip install -e '.[lyrics]'    # the ElevenLabs SDK; ELEVENLABS_API_KEY to actually run
```

## Recommendations, and what they are not

The song page ranks the hooks and says why. It is **not** a predictor of what performs —
BUILD-SPEC §7 lists that under what this project deliberately does not build, and nothing
in `services/recommendations.py` came from a model.

It is RHYTHM-STUDY §9's candidate rules, which that study wrote down and noted are "none of
these is enforced today", applied to a measured song: a loop starts on a bar line, spans a
whole number of bars, fits inside the measured plateau, and lands in the 3–10 s window the
renderer will actually cut. Each rule reports pass, fail, or **not checkable** — the third
state matters, because rendering "no measurement for this rule" as a failure sends someone
to fix a window that may well be right.

Every card carries its basis: the measurement the recommendation rests on. A song with
nothing measured gets the list of what is missing instead of a ranking of guesses, and
there is no weighted total anywhere — a single score would read as a quality judgement,
which is exactly the claim §5 shows this material cannot support.

---

## Which provider actually runs

`RK_GENERATOR_BACKEND=genblaze` selects the live path; *which* provider serves each
modality is then decided by what is installed, what is actually keyed, and any pin.

| Modality | Deployed today | Order when unpinned |
|---|---|---|
| Video | **Sora** (`sora-2`) — pinned | GMI Cloud → OpenAI → Google |
| Image | **GMI Cloud** (`seedream-5.0-lite`) | GMI Cloud → OpenAI → Google |
| Audio | **ElevenLabs** (`eleven_multilingual_v2`) | ElevenLabs → OpenAI |

A modality nobody serves is skipped with a warning rather than failing the run, because
a partial provider set is the normal state.

**Video is pinned, and the pin is the point.** `RK_VIDEO_PROVIDER=openai` moves video to
Sora even though GMI is keyed and would otherwise win. On 2026-07-31 GMI's `seedance-2`
failed twice after ~174s and billed for output it never produced; the fix had to be a
deploy-time setting rather than an edit, because a provider misbehaving in production is
an operational event, not a code change. Image stayed on GMI, which is cheap and works.
A pin naming an unavailable vendor logs a warning and falls through to the order above —
a typo in an environment variable should degrade, not silently delete video from a kit.

**The model follows the provider, not the config.** `RK_VIDEO_MODEL` and friends default
to empty; each provider names the model it actually serves in
`adapters/providers.DEFAULT_MODELS`. They used to default to GMI slugs, which was correct
only while GMI was the only live provider — the moment anything else answered, a
`seedance-…` id was being sent to Sora or Veo, which is a guaranteed 404. Set one to pin
a model, and it applies to whoever answers.

**"Keyed" excludes the placeholder.** Terraform seeds each SSM parameter with a literal
`PLACEHOLDER …` string so the slot exists before anyone has a key — Terraform must never
hold the value itself, or it lands in state in plaintext. `providers._keyed()` treats
that string as unset, because `GMICloudVideoProvider()` constructs happily with no
credential and would otherwise report a **live** generator that fails at generate time.

**Providers are baked into the image, not installed at deploy.** `Dockerfile` installs
`.[b2,queue,audio,gmicloud,openai,elevenlabs,lyrics]`. This is stated because getting it
wrong is invisible: the image once shipped with `.[b2,queue,audio]` only, so
`live_providers()` returned `{}` and every shot was skipped — and because the Dockerfile
set `RK_GENERATOR_BACKEND=genblaze` while the Lambda environment overrode it to `mock`,
the missing dependency read as a credentials problem for weeks. `google` is deliberately
excluded: Vertex authenticates with Application Default Credentials, which do not exist
in Lambda or a Fargate task, so shipping it would add weight for a provider that cannot
authenticate there.

---

## What a read costs, and what a failed one may claim

A song that was in the bucket, under the right tenant, listed correctly by the roster
beside it, returned **404**:

```
INFO: 127.0.0.1 - "GET /console/songs/sng_f438a34afbb9 HTTP/1.1" 404 Not Found
```

The song was fine. B2 had hit a daily cap:

```
Cannot download file, download bandwidth or transaction (Class B) cap exceeded.
```

B2 refuses a capped read with **403**, not 404 — and the chain from there was four layers
of individually reasonable decisions. The S3 backend's `exists()` maps 403 to `False`, on
purpose, because scoped keys holding `ReadFiles` without `ListFiles` answer 403 for HEAD on
a key that is not there. The document repository read `False` as absent. The service turned
absent into `NotFound`, and the console turned that into 404. **The catalogue took the
blame for the storage**, and B2's own sentence — the only part anybody could act on — was
discarded three frames down.

Every signal pointed away from the truth: the object was present, `list()` kept working
(listing is a different transaction class, with its own cap), and nothing in the app's logs
said "B2".

Three changes, and they are as much about cost as correctness:

* **Absence is the vendor's word, not an inference.** `DocumentRepo` reads through one
  `get`, and only a `NOT_FOUND` classification counts as missing. A cap, an expired key or
  a partition is raised. `list()` keeps tolerating a document nobody can parse — one broken
  row — but no longer swallows a bucket nobody can read, which used to render as a label
  with no songs.
* **One read per document instead of two.** `exists()`-then-`get()` billed twice and asked
  the wrong question with the first. The cap that was reached is measured in transactions.
* **Presigned URLs hold still.** SigV4 signs the wall clock to the second, so every render
  minted a new URL for an unchanged object, and a browser caches by URL — so each page view
  re-downloaded every reference frame and every clip *in full*. The bucket holds tens of
  megabytes against a daily gigabyte, which is plenty until a 5MB frame is fetched again on
  every render. A signed URL is now reused for half its life, and `put`/`delete` drop it so
  a replaced object is re-signed rather than served stale.

`preload="none"` on both players belongs to the same accounting: opening a song is not
asking to hear it, and `metadata` spent a billable read on the master every time the page
rendered — which, with htmx swapping fragments around it, is often.

Storage failing is now a **503** quoting the vendor, not a 404 blaming the catalogue.
Nothing is broken when a cap is reached; the bucket is refusing to answer for a while, and
that is a condition with a clock on it.

### One snapshot per request

B2 bills *transactions*, not bytes — 2,500 Class B a day — so what a page costs is the
number of documents it touches. Measured against one artist and five songs:

| page | Class B | distinct | repeated |
| --- | ---: | ---: | ---: |
| `/console` | 2 | 1 | 1 |
| `/console/catalogue` | 8 | 6 | 2 |
| `/console/songs/{id}` | 8 | 6 | 2 |
| `/console/artists/{id}` | **12** | 6 | **6** |

Those are floors, and the repeats are proportional: the artist page read *every* document
exactly twice. Nobody wrote it that way — the nav resolves the roster, the body resolves it
again, a panel resolves it a third time, each correct alone and none aware the last one
just paid.

So a read is remembered for the length of one request. The boundary is the request because
that is also where it is *correct*: a response whose nav disagrees with its body about a
song's title is a bug, not a saving. Nothing survives the response, so a write lands on the
next page load and there is no invalidation to get wrong — and within a request, `put` and
`delete` drop the key, because most handlers render what they just saved. A refusal is
never cached: a 403 is a fact about the storage right now, not about the key.

### Dev does not read production storage

**B2's caps are per account, not per bucket.** A laptop browsing the production bucket
spends the same 2,500 daily transactions the deployed app needs, and when they run out
every read 403s for everybody at once — including whoever you are showing the app to. A
separate dev bucket would not help. A different backend does.

Local storage costs nothing and shows nothing, though — every artist, song, identity and
kit lives in the bucket, so a laptop pointed at a directory starts with an empty catalogue.
`remixkit.tools.sync_from_b2` closes that gap by paying for the tenant **once**:

```
python -m remixkit.tools.sync_from_b2 --dry-run          # what it would cost, reads nothing
python -m remixkit.tools.sync_from_b2                    # the catalogue
python -m remixkit.tools.sync_from_b2 --media            # ...plus frames and masters
python -m remixkit.tools.sync_from_b2 --media --runs     # ...plus generated output
```

Measured against this bucket: **15** objects for the catalogue, **20** with media, **33**
with run output. A console page costs about 25 reads, so the entire tenant is cheaper to
copy once than to browse twice — and after it, nothing is spent again.

It is one-way and stays that way. This reads B2 and writes local disk, never the reverse:
the local copy is a working convenience, not a replica, and if it diverges the answer is to
delete the directory and run it again. Anything that made a laptop a candidate for upload
would turn dev into a way to corrupt production, which is far worse than an empty console.
Re-runs skip what is already local, so adding one song costs one read, and an object that
cannot be read is logged and stepped over rather than aborting the other forty-four —
a cap reached mid-sync is the likeliest failure, and the run resumes.

So `RK_STORAGE_BACKEND` defaults to `local` and the startup banner names it when a dev
process is pointed at B2 anyway:

```
Dev process is reading PRODUCTION storage (bucket 'respect-the-funk', secrets
/remixkit/prod) — B2 caps are per account, so local browsing spends the deployed app's
daily transactions. Set RK_STORAGE_BACKEND=local for routine work.
```

---

## What a run costs, and why that is a module

`adapters/pricing.py` is one dated price book, registered into Genblaze's own
`ModelRegistry`. It exists because both of the numbers this app used to show were wrong,
in opposite directions, and neither was a bug in the ordinary sense:

* Genblaze registers **no prices of its own** — "the SDK ships zero hardcoded prices as
  of 0.3.0" — so `step.cost_usd` is `None` for every unregistered model. The generator
  coerced that to `0`, and a multi-dollar run reported a `$0.00` ledger.
* A separate flat table priced a *modality* rather than a *model* and ignored duration,
  so every video cost 42¢ regardless. A three-video kit quoted $1.26 and charged several
  dollars.

One registered book fixes all three: `Pipeline.estimated_cost()` becomes real, the ledger
becomes real, and the two cannot drift because they read the same rates. A model with no
registered price estimates at `UNKNOWN_CALL_USD` rather than free — unknown pricing as
zero is exactly how the original problem stayed invisible — and `cost_cents` is now
`int | None`, so "unknown" and "free" are different claims on the ledger.

`RK_MAX_RUN_CENTS` refuses a run whose estimate exceeds the ceiling, **before** anything
is submitted. Genblaze documents no spend guardrail, and says nothing about whether a
failed step is billed; the observed answer is that it is. So the only safe assumption is
that a submitted step is a charged step, and the only safe place to refuse is before
submission.

## Running a failed kit again

A failed run used to be a terminal row. The console rendered whatever the provider said —
`Sora submit failed: Expected entry at 'input_reference' to be bytes … received <class
'dict'>` — and offered exactly one thing to do about it: delete the kit. Getting back to
where you were meant rebuilding the brief through the generate screen, which for a kit
carrying eight rewritten prompts is not something anybody reproduces exactly.

Nothing was missing except the button. `kit.brief` is what the worker re-plans from,
`run()` already rebuilds the whole request from it, and redelivery is already a no-op. So
**Retry run** re-queues the same kit — same id, same name, same brief — and the row comes
back `queued`, carrying the same 3-second poll that draws a first run.

Three things a retry can quietly get wrong, and what this does about each:

* **Charging twice.** Only a `failed` kit retries. A queued or running one is already on
  its way, and a `ready` one would spend the money again to replace assets that may
  already be approved — "Generate another" is the honest way to ask for that.
* **Losing what the failed attempt cost.** `run()` overwrites `assets` and re-costs from
  them, so a kit that burned two video steps before failing would read as though it had
  only ever cost the successful run. The discarded attempt is recorded on `kit.attempts`
  with its error, its run id and its price; `kit.spent_cents` adds it back, and the kit
  page lists every earlier attempt under the ledger. Its *objects* do not survive — they
  are deleted from the bucket at retry time, because `run()` would otherwise orphan them.
* **Being swallowed by the queue.** SQS FIFO holds a `MessageDeduplicationId` for five
  minutes. The kit id alone was the right dedupe key exactly once: a retry issued straight
  after a fast failure — which is precisely when somebody has just fixed the cause — would
  be accepted and silently dropped. The key is `kit_id:attempt`.

The consent gate is re-checked rather than inherited. Time passes between a failure and
the retry, a retry generates the artist's face again, so it has to answer the rights
question again.

`POST /api/v1/kits/{id}/retry` is the same thing over JSON — 202, and the id does not
change, so a caller polling `GET /kits/{id}` watches one document run twice.

## Seeing the run before buying it

`/console/artists/{id}/songs/{id}/generate` is a dry run. It resolves the whole brief to
per-shot wire payloads — the model that would answer, the composed prompt as the provider
receives it, the negatives, the duration after snapping, and the price — and sends nothing.

The property that makes it worth reading is that there is **one resolution path**.
`GenblazeGenerator._resolve()` builds the payloads; `plan()` displays them and `generate()`
submits them. Not two implementations kept in step by hand — one function with two callers.

That mattered on 2026-07-31. A kit for "Un Poquito Más" came back as a stock night drive
scored with a lofi instrumental the model wrote itself, and nothing had failed. The plan
table showed `Vertical 9:16 loop, moody night drive…`, which was the *mood fragment* —
truncated at ninety characters, before the identity is prepended. The string on the wire
named no song, no artist, and no constraint against audio, and no screen in the product
displayed it. Three separate defects were invisible because the payload was.

Every prompt on the screen is editable and pre-filled with what would be sent. Edits are
re-priced on the server, carried onto the queue button, stored on the kit as
`brief["shot_overrides"]`, and replayed by the worker — so what was previewed is what runs.
Clearing a field restores the generated default rather than sending an empty prompt.

Three cheaper-than-live paths exist beyond it: `RK_GENERATOR_BACKEND=mock` renders real
files and a real manifest with synthetic pixels for nothing; a proxy-frame tier and step
caching are specified but not built. See `PIPELINE-SPEC.md` for both, and for the node
model that replaces the four hardcoded moods.

### The identity builder

One surface, at `/console/artists/{id}/identity`. It used to be three: the builder page,
which split "Description" from "Reference frames" across a tab switch, plus a second full
copy of the form on the artist page. Saving mints a version, so two open screens could each
mint a version over the other from forms that were both current when they rendered — and
the tab split put the words describing a face on one pane and the photographs of that face
on another, which made "do these agree" the one question the layout prevented.

The artist page now shows a read-only summary and links here.

**Three columns, and depth for the rest.** The page has three jobs on screen at once —
what the model is told, what it is shown, and what has been said before — and they are only
useful together, so stacking them into one scroll defeated the merge that put them on one
page. The compiled prompt pins to the bottom of the column that produces it. Uploading
opens a modal, a still opens a lightbox, version history opens a drawer.

Those are native `<dialog>` elements: the browser owns focus trapping, Escape, the top
layer, and making the page behind inert, which is four things that are tedious to hand-roll
and easy to get subtly wrong. The glue is about ten lines of event delegation. `console.css`
declares a five-step elevation scale (`--z-sticky` … `--z-lightbox`) and every `z-index` in
the file is one of them — a test asserts no bare integers, because the failure mode of
stacking is invisible until two things collide and the fix is a bigger number, forever.

**A turntable, not a contact sheet.** The reference set renders as one image at full size
with a 3×3 grid of hotspots over it, so moving the pointer turns the head: horizontal
movement rotates, and the top row is the far side. A grid of thumbnails asks you to
assemble a person out of six pictures in your own head; the turntable does the assembling,
which turns "do these look like the same human" from something you reason about into
something you see. That is the question a reference set has to answer before it is worth
conditioning on, and nothing in the console asked it.

An uncovered class shows a gap panel rather than nothing — so a missing back-of-head is
*felt*, as the head refusing to turn, instead of being findable only by reading a count.
It is pure CSS: the hotspots sit before the stage in the DOM and `:has()` does the reveal,
because a sibling combinator only looks forward and building it the intuitive way round
(pictures first) is exactly what would have needed JavaScript.

**Several faces per artist.** An `Artist` is a roster entry, and a band is one entry with
four faces in it. `Identity.name` is the *line*; versions count within their line, so a
band's second member starts at v1 rather than being numbered after somebody else's
revisions. The empty name is the primary line — what every solo artist has and what every
identity stored before the field existed is — and `current()` with no name still returns
it, so adding a member cannot silently change whose face existing kits render. Asking for a
line that has no versions returns `None` rather than substituting a different person, which
is the one substitution that must never happen.

**Full CRUD.** Frames can be removed, not just added — a mis-classified upload used to be
permanent, and a profile filed as a front kept winning the one conditioning slot for every
kit afterwards. Versions can be approved, deleted, and restored. Restoring *copies forward*
rather than rewinding: a version rewound in place would leave a kit generated last week
pointing at a version number whose content had since changed underneath it.

Two deletion rules follow from that and are easy to get wrong. Removing a frame deletes its
object only if no other frame in the identity shares the key — the key is the content digest
alone, so one photograph filed under two classes is two rows sharing one object. Deleting a
*version* deletes only the objects no surviving version references, because restore copies
frames forward by reference. Without either rule nothing errors: the rows remain and the
images 404, which is the shape of data loss that is hardest to notice.

**The description is standardised where it can be.** Presentation, build and height are
enums picked from chips and a row of drawn silhouettes rather than typed as prose. The
silhouettes are parameterised — three half-widths per build, scaled by presentation — so
the whole picker is one macro and a table of numbers rather than fifteen drawings. There is
one radio per build with the drawing swapped underneath it, because a set of radios per
presentation loses the selection the moment presentation changes.

Presentation rather than gender, deliberately: what conditions a render is silhouette and
styling, neither of which is recoverable from a demographic field, and this is a record of
how an artist presents in their own work rather than a claim the label is making about
them. `unspecified` is the default and contributes nothing to a prompt.

**Reference frames are classed.** Six angles — three-quarter left/right, front, profile
left/right, back — crossed with the lighting label that already existed. The frames grid
groups by class and shows every class including the empty ones, because a flat grid answers
"how many" and hides the only question that matters, which is which views are covered.

**And the whole thing is bounded.** See below.

### The prompt budget

`Identity.compile()` renders the identity into at most `PROMPT_BUDGET` characters (220),
ordered by salience — silhouette, then face structure, then wardrobe — and drops clauses
*whole* when they do not fit. `prompt_fragment()` is `compile()`, so the budget is a
property of the run rather than a decoration on a screen.

This is the compression argument, and it is a real failure mode rather than tidiness. A
prompt is a fixed budget of attention; every character the identity spends is one the shot
does not get. An identity written as three paragraphs pushes "vertical 9:16 loop, clean
centre frame left empty" far enough down the string that the model renders a portrait
instead of a backdrop — and that reads like the shot description was ignored, because
effectively it was.

Clauses drop whole because a prompt ending in `high cheekb` is worse than one that never
mentioned cheekbones: a fragment is not less information, it is a different instruction.
Ordering is truncation policy — silhouette survives anything, wardrobe goes first, since
wardrobe is the most likely to be overridden by the shot and the cheapest to lose.

The builder shows the compiled string, its length against the budget, and a count of what
was refused. An identity that silently loses its wardrobe every render is one somebody will
keep editing without understanding why nothing changes.

Reference frames cost nothing against this budget, which is the trade the screen is trying
to make obvious: detail that will not fit in the text belongs in a photograph.

### Holding the artist's face

`Identity.reference_frames` had been on the model since it was written, and nothing read
it — `_compose_prompt` was text-only, so a label that photographed an artist across four
lighting setups was buying video conditioned on a sentence. The frames now go to the
provider as an actual image.

`Identity.plates()` picks which ones: exact duplicates dropped by content hash, then one
frame per lighting setup, hashed frames preferred. Ordinary bookkeeping, no new dependency,
and pure — so the plan screen can call it on every render. Upload as many stills as you
like; variety across *setups* is what makes the chosen plate generalise, which is what
`ReferenceFrame.lighting` has always been for.

A video shot takes exactly **one** plate, and that is a correctness limit rather than a
budget: Seedance routes a second image into `last_frame`, which the model reads as "end the
clip here" and interpolates towards. Lyric cards take none — a face reference on a
typographic plate gives you a portrait with words over it.

Three things will stop a plate being sent, and the plan screen names whichever applies
rather than going quiet:

- **The provider refuses image inputs.** GMI and Sora declare `accepts_chain_input=True`;
  Veo and Imagen declare neither, and Genblaze raises before *any* step runs — so an
  ungated plate fails the whole kit, not one shot.
- **Storage does not serve public URLs.** The provider fetches the image from its own
  network, and `LocalStorage` presigns to `/files/…`, an app route that means nothing
  off-host. A live backend on local storage refuses instead of sending a dead link. The
  mock generator fetches nothing and is exempt, which is what keeps this path exercised on
  a laptop.
- **The identity has no frames.** Text-only, and it says so instead of looking conditioned.

### The identity lock — getting the artist, not a description of them

A video model conditioned on text drifts across the clip: the face at second six is not
the face at second one, because nothing anchors it. A video model conditioned on a *first
frame* is anchored — it inherits the identity rather than re-deriving it.

So a loop is rendered in two stages. Stage one is a still, conditioned on the artist's
reference plates, costing roughly a fifteenth of the clip. Stage two is the clip, generated
from that still via `input_from`, which lands in Seedance's `first_frame`. The expensive,
uncertain part happens once, cheaply, where a person can look at it and regenerate until it
is right.

A locked clip carries **no** plates of its own. Seedance's slots are positional, so a clip
handed both the still and the raw reference has two different images in `first_frame` and
`last_frame`, and is being asked to interpolate between them rather than to hold a face.

The lock degrades rather than failing: no image provider, no reference frames, or a
provider that refuses image inputs, and you get a plain plate-conditioned clip with the
reason on the plan row. `input_from` is validated under the same capability rule as
`external_inputs`, so a provider that refuses images refuses the still too — building one
anyway would render and bill a frame whose only consumer then kills the run.

Both stages are priced. The plan header shows shots *and* provider calls, because a
two-stage shot is one thing you bought and two things you are charged for.

**How far this goes.** Four techniques produce a recognisable person; only two are reachable
from this stack. The two-stage lock is built. Multi-reference conditioning is plumbed but
needs a verified payload key (below). Character fine-tuning has no training API on any of
the five installed connectors. Face swap has no model in the GMI registry — the image
families are inpainting and image-to-image edit — and would mean `inswapper`-class weights
on our own compute against the delivered MP4. `PIPELINE-SPEC.md` §6b has the full table.

### The still index — why the second kit is cheap

The two-stage lock makes the still the expensive half: it is where the face is resolved,
and the clip merely inherits it. A still is also a *pure function of its inputs* — who is
in it, which version of each of them, and the scene — so rendering the same one twice is
paying twice for an answer already stored.

`Identity.locked_stills` is that index, keyed by `still_digest(identities, prompt)`. A kit
that hits it skips the image call entirely: nothing is submitted, nothing is billed, the
plan row reads `reusing a stored still, free`, and the clip is conditioned on the stored
frame through `external_inputs` rather than through `input_from`.

Identity **versions** are in the digest deliberately. Saving a new version of a face must
*miss* the index rather than reuse a frame of how that person used to look — that is the
one way a cache here could quietly undo the versioning discipline the rest of the model
keeps. The scene is in it too, so a night-drive still is not offered for a golden-hour shot.

The index lives on the identity line rather than on the kit, because its whole value is
outliving the kit that paid for it. It is MEMORY-SPEC's "the second video is cheap" applied
to the step that actually costs.

### Several faces in one kit

`Identity.name` gives an artist more than one line; `kits.request(identity_names=[…])`
chooses which of them a kit is for, and it accepts more than one. Each face's compiled text
composes into the prompt under its own name — two unlabelled silhouettes read to a model as
one contradictory person, where `Amanda Kurt: … . Marco: …` reads as two people. The
primary line has no name of its own, so in a group shot it is labelled with the artist's.

Omitting `identity_names` means the primary line alone, which is what every kit meant before
lines existed — so a solo artist is unchanged and adding a band member cannot alter whose
face an existing kit renders. A named line with no versions is dropped rather than
substituted: a kit that quietly renders a different *person* than its brief says is worse
than one that renders one fewer shot.

The brief records every face by id *and* name, so "which member was this kit for" is
readable without a lookup.

**The plates are the limit, not the list.** Each face contributes one reference and the
model's slot count caps the total — so today a duo sends one plate and the plan row says
`2 faces — 1 reference sent`. A duo silently conditioned on one member is exactly the
failure that must not be invisible.

What follows that row is the *reason*, because only one of the two is actionable. An
unstated slot count is a configuration question — `; set RK_REFERENCE_SLOT to send the
rest`. A count the vendor has published is not — `; bria-fibo-edit takes 1`, and no setting
will move it. Advising a knob that cannot change the number is worse than saying nothing:
it gets set, the count is identical, and the row stops being believed.

### Both stages have to read the reference

Two models sit between an artist's photograph and a finished clip, and **each one can
silently discard it**. Getting the first right changed nothing until the second was too.

| stage | GMI default | hub tags it | result |
|---|---|---|---|
| still | `seedream-5.0-lite` | Text-to-Image | reference ignored |
| clip | `seedance-2-0-260128` | **Text-to-Video only** | the still is discarded |

The second is the one that hid the first. The connector declares `first_frame`/`last_frame`
for every `seedance-*` slug — true of the 1.x line, not of 2.0 — so the lock rendered a
frame, paid for it, and handed it to a model that throws it away.

Set both, from GMI's Model Hub rather than from any list in this repo:

```
RK_IMAGE_EDIT_MODEL=bria-fibo-edit                  # or gpt-image-2-edit, seededit-3-0-i2i-250628
RK_VIDEO_EDIT_MODEL=kling-o1-reference-to-video     # or kling-o1-image-to-video, vidu-q2-pro-i2v
```

`kling-o1-reference-to-video` is the one shaped for this problem — reference-to-video
rather than animate-this-frame. `GMI-MiniMeTalks-Workflow` ($0.02, Image-to-Video) is worth
trying for a talking head specifically.

Unset, the plan screen names the missing setting and says no likeness is held. Nothing is
guessed: a hardcoded slug 404'd once already, and GMI declares `DiscoverySupport.PARTIAL`
so there is no catalog endpoint to check one against.

### Accepting a reference and reading one

`GMICloudImageProvider` declares `accepts_chain_input=True`, so the capability gate passes
and a reference image is attached. That says nothing about whether the **model** reads it.
GMI's default image model is `seedream-5.0-lite`, which is text-to-image: the payload
carried an `image` key nothing consumed, so the only thing describing the artist was the
compiled silhouette, and what came back was a stranger with the right build. It cost full
price and looked like it had worked.

`providers.honours_reference()` is the model-level check the provider-level one could not
be. Only the shipped registry's own image-to-image families qualify — Seededit, Reve
edit/remix, Bria inpaint — plus FLUX Kontext and OpenAI's image edit. **An unknown model
answers no**, which is the safe direction: a model wrongly treated as image-capable renders
a plausible stranger and bills for it, where one wrongly treated as text-only produces a
refusal on the plan screen that somebody can act on.

**Any shot that wants the face swaps the model; it does not drop the reference.** That
distinction is the difference between the first attempt at this fix and the one that works.
Refusing the plate made the failure legible and still rendered the picture — a portrait
still went out on `seedream-5.0-lite` with no reference attached and came back as the same
stranger it had produced before, now with an explanation nobody was reading.
`providers.reference_model()` swaps to **`RK_IMAGE_EDIT_MODEL`**, and there is no default.

That absence is the correction to a second mistake. `seededit-3-0-i2i-250628` was hardcoded
here and 404'd on a real account — `InvalidEndpointOrModel.NotFound` — because the slug came
from the connector's `example_slugs`, which its own docstring calls *"documentation grade,
not a contract"*. GMI media providers declare `DiscoverySupport.PARTIAL`: there is no
catalog endpoint to check a slug against, and the only liveness test is the empty-payload
probe, which is a billable generation and is off. Which image-to-image models an account
can reach is a fact only the operator has, so guessing again would just move the 404.

`providers.REFERENCE_MODEL_CANDIDATES` lists what to look for in the GMI console. Until the
variable is set, the plan screen says the reference will not be held and names the setting
— a refusal before the button rather than a failed run.

An explicitly pinned per-shot model is never swapped: somebody who typed a model into the
plan meant it, and silently running a different one would defeat the field. The refusal
note still says what it costs them.

The check reads the modality of the *step carrying the reference*, not the shot's own. A
locked video shot hands its plate to the still in front of it, so reading `shot.modality`
there says VIDEO — and the check silently never ran on the one step whose entire job is to
resolve the likeness.

**`seededit-3-0-i2i-250628` is unpriced**, so it estimates at the unknown-model rate of
$2.00/call. That is deliberate and it bites: three locked clips quote at 783¢ and are
refused by the 500¢ `RK_MAX_RUN_CENTS` ceiling. Reconcile the real rate against a GMI
invoice and register it in `adapters/pricing.PRICE_BOOK` — the same discipline every other
model in that table went through. Until then, the refusal is the system working.

### Payload shapes the connector does not ship

Genblaze routes input assets per model family, and a model with no matching family falls
through to a permissive fallback emitting `{"image": <url>}`. When the vendor wants
something else the request is rejected — or accepted and ignored.

`adapters/model_families.py` corrects those, and every entry is **evidence rather than
inference**. That distinction is why the module exists: two earlier attempts guessed a slug
and a slot name from the connector's `example_slugs` and both failed in production. A
rejection that names the missing parameter cannot be wrong about it.

```
bria-fibo-edit -> 400: invalid payload parameters: images (Required parameter is missing)
```

So Bria's fibo family takes `images` as an **array**, and the connector ships a family for
`bria-genfill`/`bria-eraser` only. Registered on the same forked registry as the prices,
checked before the connector's own, and never overriding a more specific shipped family.

The array's **length is a separate fact**, and reading it off the plural cost the next run.
`images` was taken as the multi-reference answer `RK_REFERENCE_SLOT` had been waiting for,
the classed set went out four frames wide, and the vendor said:

```
bria-fibo-edit -> 400: invalid payload parameters: images
                       (Number of images 4 exceeds maximum allowed value 1)
```

`maxItems: 1`. An API states a container and its capacity separately, and only one of them
had ever been stated to us — the plural named the key, never how much it holds.

So the count is a property of the *model* and is quoted rather than derived:
`providers.REFERENCE_LIMITS` carries it, the family's mapper trims to it, and it is a
**ceiling before it is an allowance** — it caps `RK_REFERENCE_MAX` too, because an operator
cannot raise a vendor's maximum and a request over it dies after the plate is rendered and
the queue slot is spent. A model nobody has measured declares nothing and falls back to the
deployment default rather than inheriting a neighbour's allowance.

### Sora's start frame is an object, not an upload

The same correction one layer up, on the other vendor. `genblaze-openai` uploads the start
frame as a file, which is what the Videos API used to take:

```
Step 1 (openai-sora/sora-2): Sora submit failed: Error code: 400 -
  Invalid type for 'input_reference': expected an object, but got a file instead.
```

So it wants an object. Handing one to `client.videos.create()` was then refused by the SDK,
before any request left the process:

```
Expected entry at `input_reference` to be bytes, an io.IOBase instance, PathLike
  or a tuple but received <class 'dict'> instead.
```

Which reads like a contradiction and is not. `Videos.create()` unconditionally sets
`Content-Type: multipart/form-data` and unconditionally runs
`extract_files(body, paths=[["input_reference"]])`, which *pops* the value and asserts it
is file content. The generated method implements only the file half of a parameter its own
type declares as a union, so the object is not a choice it offers.

**The spec settles it.** From the OpenAPI document the SDK is generated from (`.stats.yml`
→ `CreateVideoMultipartBody`), `POST /v1/videos` declares exactly one request content type:

```yaml
input_reference:
  anyOf:
    - {type: string, format: binary}          # the upload
    - $ref: ImageRefParam                     # "exactly one of image_url or file_id"
```

`ImageRefParam.image_url` is *"a fully qualified URL or base64-encoded data URL"*, capped at
`maxLength: 20971520`. JSON was never on the table — the object is a **multipart field**,
not a JSON body, and the SDK's own serializer brackets nested values, so the field is
`input_reference[image_url]`.

That encoding is not invented here: it is what `BaseClient._serialize_multipartform`
produces from the same spec, and the tests build the real request and read the field name
back off the wire. Three shapes were shipped against a stubbed client and all three passed
their tests; asserting on bytes is what finally had teeth.

`adapters/provider_sora.py` subclasses the provider and overrides `submit` — that one
parameter, nothing else — issuing the request one level below the typed method so the body
can say what the spec allows. Auth, base URL, retries, response parsing, validation, SSRF
checks, seconds and size handling, error mapping, discovery, polling and download all stay
inherited, and a shot with no reference never leaves the connector's own typed path.
**When `videos.create()` learns to send the object arm, deleting the module should be the
whole of the change.**

The two kinds of reference are not interchangeable, and telling them apart is most of the
file:

| arriving as | is | sent as |
| --- | --- | --- |
| `https://…` | a plate, presigned out of B2 | `image_url`, untouched — the address is the point |
| `file://…` | a chained still, before the sink has uploaded it | a base64 data URL, since there is no address to give |

The connector downloaded the https case and re-uploaded the bytes. A URL the API accepts
makes that round trip pure cost, and for a user's photograph it was megabytes of it.

### Live re-pricing

Every control on the generate form changes what a run costs, and a page that only tells you
after a reload is a page people stop reading. The form re-prices itself with htmx on any
change — still a GET to the same handler, so the estimate is still the server's answer and
there is no second implementation of the arithmetic.

Two regions swap: the plan, and the pagebar stats out-of-band, because the estimate lives
outside the scrolling area on purpose — the number that decides whether to press the button
must not scroll away from it.

The debounce is on text inputs only. A tick or a radio is a finished decision; a prompt
being typed is not, and re-pricing per keystroke would be a request per character for an
answer nobody is reading yet.

### `RK_REFERENCE_SLOT`

Sending several reference frames in one call is the standard zero-shot way to hold a
likeness, and the frames are already collected and classed. The plumbing is built. The
missing piece is a fact about the vendor, not about this code.

Genblaze routes any number of images — `route_images(array_slot=…)` works. What it cannot
know is the key a given model expects them under, and GMI ships no per-model payload
schema: every image model resolves through a fallback declaring `route_images(slots=
("image",))`, one positional slot, and the mapper's documented behaviour for the rest is
*"If `array_slot` is None, extras are dropped."*

So one reference is the only count this code can honestly claim, and that is the default.
Inventing a key here gives you a 400, or — much worse — a kit that reports four references,
is billed for one, and looks conditioned.

Set `RK_REFERENCE_SLOT` once you have checked the model's actual payload in GMI's
catalogue. The whole classed plate set then goes, and the slot name and count are written
into the step metadata, so a wrong guess is visible in the run's own record rather than
only in a face that came back looking like someone else.

It cannot exceed a limit the vendor has stated, though. `bria-fibo-edit` takes one image
whatever this is set to — the setting says how many frames a deployment *wants* to send,
never how many the model will accept, and the plan row names the model rather than this
knob when the model is what capped the count.

### Formats

A kit is an instance of a **format**, and formats are rows. `Recipe` carries the prompt
template, the negatives, the aspect ratio, the length policy, the face policy, which
reference class the framing wants, whether the master goes under the result, and the
variant spread — so adding a format is adding a record rather than editing the planner.

This is also the answer to a question the repo used to get wrong: RemixKit generates the
media a release needs, whatever kind that is. It is not a manufacturer of templatable UGC
bait that happens to do other things — see PRODUCT.md (deleted 2026-08-15; in git history) § *What the product
generates, precisely*. The backdrop below is one row.

Five ship, seeded per tenant on first read (there is no migration step to hang a fixture
on, and a tenant is created by the first request that mentions it). Built-ins refuse to be
deleted, because they are the floor a tenant falls back to; copy and edit the copy.

| Format | Face | Reference class | Length | Master under it | Suppresses |
|---|---|---|---|---|---|
| Backdrop loop | none | — | hook window | yes | music, speech, singing |
| Direct address | locked | front | fixed 8s | **no** | *nothing audio* |
| Performance clip | locked | three-quarter | hook window | yes | music, speech |
| Portrait still | plate | front | — | — | — |
| Voice-over | none | — | — | — | — |

**The default is `performance`, not the backdrop.** The backdrop was the only format this
app had and it is the wrong default now there is a choice: it deliberately has nobody in
it, so defaulting to it would mean an artist's identity — the frames, the silhouette, the
locked still — reaching nothing unless somebody went looking for the right format.

`tts_text` is a voice-over recipe planned alongside, rather than a branch in the video
planner. That is what it always was; making it a format means it gets the same prompt,
provenance and preview treatment as everything else.

### What the generate screen can change

Everything that alters the plan is on it, and everything on it rides the queue button —
a control that changes what is bought without changing what was priced is the divergence
this screen exists to prevent.

**Kit-level:** the format, the spoken line when the format has one, count, which hooks to cut to, **which faces the kit is for**, an
optional voice-over line, a budget ceiling, and the kit's name.

**Per shot:** the composed prompt, the model, the loop length, and whether the face is
locked to a still at all. Length had been overridable since per-shot editing landed and
had no field — the one thing a loop is measured in was the one thing the plan could not
change. Turning the lock off is for a backdrop that wants nobody in it, and it skips the
still rather than rendering one it will not use.

The budget ceiling refuses *on the screen*: `KitService.request` raises on an estimate over
it, and a button that submits into a known refusal is a round trip to be told something the
page already knew.

Two encoding details in the face picker, both load-bearing. Not choosing and choosing
nobody are different answers — the first means the primary line, the second means a kit
with no face — and an HTML checkbox group sends nothing in either case, so a hidden
`faces_chosen` marker distinguishes them. That marker cannot be an empty `identity_names`
value, because the empty string is *the primary line's real name*: using it would silently
add the primary to every kit and make "Marco alone" unreachable. And the primary is
pre-ticked on first load, because the boxes have to describe the plan underneath them.

### The master under the picture

For most formats the picture sits under the master. A clip that comes back carrying a
soundtrack the model composed for itself is the 2026-07-31 defect — a night drive scored
with a lofi instrumental, over a song the model had never heard.

Two halves, and only one of them was ever going to be enough.

**Asking.** Video shots carry `music, soundtrack, singing, speech, …` as `negative_prompt`
and a `silent footage` clause in the prompt. This is best-effort and the console should not
be read as promising more: `negative_prompt` is a conditioning signal, and no provider here
documents it as binding on the audio track — Veo, Sora 2 and Seedance all return video with
a native audio track by default.

**Doing.** `services/scoring.py` writes a second object next to every video a kit produces:
the same frames, with the uploaded master's own seconds mapped in place of whatever came
back.

```
ffmpeg -i clip.mp4 -ss <hook start> -i master.wav \
       -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -af apad -shortest scored.mp4
```

The picture is stream-copied, so the frames delivered are the frames the provider rendered
and the manifest's content hash still describes them. `-map` is exhaustive: the model's
audio is not muted, it is absent. The cut starts at **that shot's** hook — a brief naming
two hooks deals its loops across them, so `ShotSpec.hook_start_ms` travels through the plan
onto the asset — and runs as long as the picture does, padding silence rather than wrapping
back to bar 1.

`Asset.key` is never overwritten; the scored cut is `Asset.scored_key`, and
`Asset.playable_key` is what the console plays and what `/download` hands back
(`X-RemixKit-Audio: master`). A kit that ran before this landed is scored on the way out
instead, without storing anything.

**Not every format wants this.** `-map` being exhaustive is the point for a picture that
sits under a record and the whole problem for one whose audio *is* the asset. A direct-address
clip is bought by instructing a model to speak a line; scoring it deleted the line and
returned a mute performance with the song over it. `Recipe.score_with_master` decides, and
it is checked on both paths — in the worker, and again in `scored_now`, which exists to
score kits the worker left alone and would otherwise have done it on the way out of the
door. Every uncertain case (no slug on the asset, a format since deleted) resolves to
scoring, because a clip wrongly scored is visible on the review screen and a clip wrongly
left alone looks like the provider's fault.

Every video says which happened on `Asset.audio_note`, including the ways it can fail: no
master uploaded for the song, or no `ffmpeg` in this process. A clip silently unscored and
a clip scored by the model look identical on screen, which is how the original defect
survived being looked at. `RK_SCORE_WITH_MASTER=0` turns the whole thing off, and says so
on the asset too.

## Two front doors

```
/                    the landing page — public, always
/verify              provenance checking — public, on purpose
/auth/login          the sign-in card
/console             the roster — and everything under it
/console/catalogue          what is missing, across collections
/console/settings           which backends are live, and what each gap needs
/console/artists/{id}
/console/artists/{id}/identity                    the identity builder
/console/artists/{id}/songs/{sid}/generate        cost before the button
/console/songs/{id}
/console/kits/{id}
/api/v1/*            JSON, Authorization: Bearer
```

Two routes sit outside `/console`, and both are deliberate: `/` is the marketing page,
and `/verify` is public because PRODUCT.md keeps it usable by a judge with no account.
It reads no tenant data — `manifest.verify()` runs over uploaded bytes. It is *not* at
`/console/verify`, because a URL under that prefix promises a session check that this
handler does not perform.

`/` is the marketing site (`ui/templates/pages/landing.html`, moved here from `web/`)
and it is the **only** page whose route does not take `CurrentPrincipal`. That is the
entire gate: every console handler asks for a principal, so every console handler
refuses without a session, and no route contains an `if authenticated` branch.

It is one app rather than two because `docs/web.md` costed the alternative: a separate
front end is a second deploy target, a second dependency tree, and a $20/mo Vercel
commercial-ToS seat — the whole always-on floor of this stack, spent on a page that
never re-renders. The page needed no rework to move; it was already valid Jinja.

The nav swaps **Sign in** for **Open console** when the `rk_session` cookie verifies, so
an operator who is already signed in is not sent to a login form that would bounce them
straight back out again.

---

## Authentication: passwordless email codes

`RK_AUTH_BACKEND` selects a provider. `none` (the default) is `AnonymousAuth` — a
complete implementation that admits everyone, so a fresh checkout has no login to get
past. `otp` is the real one: a six-digit code by email, exchanged for a signed session.

```
POST /api/v1/auth/request-code   {"email": "…"}          → emails a code
POST /api/v1/auth/verify-code    {"email": …, "code": …} → {"token": "…"}
GET  /api/v1/auth/me             Authorization: Bearer … → the principal
```

The console uses the same tokens through an httpOnly `rk_session` cookie set at
`/auth/login`. One `AuthError` handler in `main.py` decides what a failure looks like:
a browser is redirected to the login page with `?next=` preserved, an htmx request gets
`HX-Redirect`, and a script gets 401 JSON. No route contains an auth branch.

Turning it on:

```bash
RK_AUTH_BACKEND=otp
RK_MAIL_BACKEND=resend
RK_ALLOWED_EMAILS=you@agfarms.dev,colleague@agfarms.dev   # this IS the user table
RK_SESSION_SECRET=$(python -c 'import secrets;print(secrets.token_urlsafe(32))')
RK_RESEND_API_KEY=re_…                                    # in prod: SSM /resend/api-key
RK_MAIL_FROM=rtp@mattrickslauer.com                       # on a domain verified in Resend
```

Four choices worth knowing about, each with a defensible opposite — the reasoning is in
`services/accounts.py`:

* **Allowlist, no registration.** `RK_ALLOWED_EMAILS` decides who exists. It is checked
  when a code is requested, when it is verified, *and* on every session read, so
  removing someone ends their session at their next request rather than at its expiry.
* **A refused address is told so**, rather than silently accepting. This leaks
  membership, which is the right trade when the population is a handful of known
  colleagues and the real failure is someone mistyping their own address.
* **Sessions are stateless** — a signed token, not a row. So there is no revocation
  before expiry; the TTL is a week, and the panic button is rotating
  `RK_SESSION_SECRET`, which signs everyone out at once.
* **Codes are stored hashed** (HMAC, keyed by email), single-use, ten-minute window,
  five attempts, thirty-second resend floor.

With no mailer configured the code goes to the server log and — *only* in `RK_ENV=dev` —
back to the caller, so the whole flow is completable on a laptop with no credentials.
Both conditions are required: either alone would be an authentication bypass on a
misconfigured deployment.

Two guards, both refusing to start rather than warning: `RK_REQUIRE_AUTH=true` with
`RK_AUTH_BACKEND=none`, and `RK_REQUIRE_AUTH=true` with an empty `RK_SESSION_SECRET`
(which would otherwise sign sessions with a key that changes every restart).

In prod these are Terraform variables rather than a `.env`, and
`infra/terraform/envs/prod/variables.tf` now defaults to `auth_backend = "otp"` and
`require_auth = true`. Put `/remixkit/prod/SESSION_SECRET` and the shared `/resend/api-key` into SSM **before**
the first apply — with `require_auth` on, a missing signing key is a function that does
not boot, which is the intended failure but an opaque one if you were not expecting it.

What made this cheap: every request already resolved a `Principal`, and every document,
object key, and job payload already carried its `tenant_id`. Adding auth was one
provider, one service, one page, and one exception handler — no service, no existing
template, and no storage key changed shape.

---

## The two refusals

Both are product features rather than validation, so both are in the service layer and
both are tested.

**No likeness consent, no kit.** A kit holds the artist's face invariant across every
shot, and that needs recorded, auditable rights. PRODUCT.md gap #3 inverted for this
use case: `nate-test` kept a face *out* of all 22 frames because rights were unknown; a
label generating content about its own signed artists wants the opposite. Withdrawal is
as easy as granting and takes effect on the next kit, not retroactively.

**No BPM without a method.** FORMAT-SPEC requires the provenance of a measurement.
PRODUCT.md names catalogue onboarding — not compute — as the real bottleneck; a field
that silently accepts an unsourced `128` hides that instead of measuring it.

---

## Provenance

The claim is "disclosure travels inside the file", and it is tested as a loop
(`tests/test_provenance.py`):

1. Generate a kit → assets + manifest land in the bucket, `manifest.verify()` gates
   the kit to `ready`. An unverified manifest fails the kit rather than shipping a
   claim we cannot back.
2. `GET /api/v1/kits/{id}/assets/{id}/download` → the delivered copy has the run's
   manifest **embedded in the media**, and says so in `X-RemixKit-Provenance`.
3. `POST /api/v1/verify` with that file → `verified: true`, `source: "embedded"`, plus
   the provider, model, and prompt of every step. Nothing is looked up.

The stored object stays byte-identical to what the provider returned — that is what
makes the content hash mean anything — so embedding happens on delivery, not before.

---

## What is deliberately not here

Everything PRODUCT.md defers: attribution links, `/r/{code}`, the disclosure gate,
leaderboards, rewards, composite sessions, K-factor. Those are role 2, and they are
what would force a relational database. Their absence is why there is no database tier.

Also absent by design: a websocket, a CDN, and a "which archetype goes viral"
recommender — research calls that folklore.

The console is still live without one. A mutation names the collection it changed in an
`HX-Trigger` header (`remixkit/ui/events.py`), and each page names the regions it keeps
current in a `live` block; one hidden element re-reads the page and swaps just those
regions out of the response, so attaching a song updates the nav tree, the header counts,
the tab strip and the catalogue in the same breath as the list. That is push to the
person who acted, which is where every staleness bug was actually seen — a second
editor's change still arrives on your next navigation. Long-running jobs keep polling
their own row every 3s, and announce themselves once when they land.
