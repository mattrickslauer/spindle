"""The console.

Server-rendered Jinja + htmx, in the same FastAPI app — BUILD-SPEC §2's frontend
decision and docs/web.md's stated precedent, which resolves open decision §13.5.

"Componentized" here means every fragment under `templates/components/` renders
standalone and is addressable by a route that returns *just that fragment*. htmx swaps
them in place. So the same `_kit_row.html` is used by the full page render, by the
create-kit response, and by the status poll — one definition, three call sites. If this
is later replaced by a React or Next.js front end, these routes are already the
component boundaries, and `api/v1.py` is already the JSON to drive them.

Every handler resolves a `Principal` even though nothing is authenticated, so the
tenant it reads is the tenant a real login would supply.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import quote

from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from remixkit.deps import (
    Accounts,
    Analysis,
    Artists,
    Catalogue,
    CurrentPrincipal,
    Identities,
    Kits,
    MaybePrincipal,
    Recipes,
    Recommendations,
    Songs,
    Transcription,
    Verify,
    get_container,
)
from remixkit.domain.models import (
    AnalysisStatus,
    ApprovalState,
    BodyBuild,
    FrameAngle,
    HeightBand,
    Presentation,
    ReferenceFrame,
    SectionRole,
)
from remixkit.adapters import scoring as scoring_mux
from remixkit.services.briefs import OVERRIDABLE, POSITION_KEY, hook_windows
from remixkit.services.recipes import DEFAULT_SLUG
from remixkit.services.errors import Conflict, ServiceError
from remixkit.ui import events
from remixkit.ui.events import announce

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def asset_url(asset) -> str:
    """A URL the browser can fetch *now*, minted at render time.

    The kit document stores the URL Genblaze reported when the asset was written, and
    on B2 that is a **presigned** URL with an expiry. Persisting it means the console
    looks correct immediately after a kit finishes and then quietly fills with broken
    tiles once the signature lapses — the kind of bug that only shows up in a demo,
    long after the code that caused it.

    So the durable thing is the key, and the URL is derived from it on every render.
    On local storage this is the `/files/...` route and costs nothing.

    `playable_key` rather than `key`, so a video plays the cut with the master under it.
    The raw provider output is still the thing the manifest hashes and is still in the
    bucket; it is just not what anybody should be shown, since a loop without the record
    is the one defect this product exists to avoid shipping. `getattr` because this helper
    also renders reference frames, which have a key and nothing derived from it.
    """
    container = get_container()
    key = getattr(asset, "playable_key", None) or getattr(asset, "key", None)
    if key:
        try:
            return container.storage.presign_get(key, expires_in=3600)
        except Exception:
            log.warning("could not presign %s", key)
    # `getattr` rather than `.url`: this helper also renders ReferenceFrame, which has a
    # key and no url. Reaching for a missing attribute here would turn a presign failure
    # into a 500 on the identity page.
    return getattr(asset, "url", "") or ""


templates.env.globals["asset_url"] = asset_url


def master_url(song) -> str:
    """A URL the browser can play this song's master from, minted at render time.

    The same rule as `asset_url`, for the same reason: on B2 this is a presigned URL with
    an expiry, so the durable thing is the key on the song and the URL is derived from it
    on every render. A song with no master returns the empty string, which is what the
    player renders as "nothing to play yet" rather than as a broken element.

    It is a template global rather than a route's context because the sections list, the
    master panel and the arrangement are all swapped independently by htmx, and each of
    them can carry a play button — threading a URL through five handlers would mean a
    fragment that quietly loses playback the first time somebody adds a sixth.
    """
    key = getattr(song, "master_key", None)
    if not key:
        return ""
    try:
        return get_container().storage.presign_get(key, expires_in=3600)
    except Exception:
        log.warning("could not presign master %s", key)
        return ""


templates.env.globals["master_url"] = master_url

# The `hx-trigger` list every page's live-refresh element carries, built from the event
# vocabulary rather than written out in the layout — so adding an event makes it live on
# every screen at once instead of being a template edit somebody forgets. `from:body`
# because `HX-Trigger` fires the event on the element that made the request, and the
# element listening is somewhere else entirely; body is where they meet.
templates.env.globals["live_events"] = ", ".join(f"{name} from:body" for name in events.ALL)

router = APIRouter(tags=["console"])
log = logging.getLogger(__name__)


def _viewer(request: Request) -> dict | None:
    """Who the layout should say is signed in, read from the session cookie.

    Deliberately not the route's `Principal`: adding a parameter to twenty handlers so
    the topbar can render a name would put auth into every signature for a decoration.
    This reads the same signed token the provider validates — `read_session` verifies
    the HMAC and the expiry and does no I/O — so the menu cannot show a name that the
    next request would reject.
    """
    container = get_container()
    token = request.cookies.get(container.settings.session_cookie, "")
    return container.accounts.read_session(token) if token else None


def _render(request: Request, template: str, **ctx) -> HTMLResponse:
    container = get_container()
    return templates.TemplateResponse(
        request,
        template,
        {
            "env": container.describe(),
            "ApprovalState": ApprovalState,
            # The photo classes, so the upload form offers exactly the set the model
            # defines. A hand-written `<option>` list is a second definition of the same
            # enum, and the two drift the first time a class is added.
            "FrameAngle": FrameAngle,
            "viewer": _viewer(request),
            **ctx,
        },
    )


def _nav(principal, *, artist=None, song=None) -> dict:
    """What the sidebar draws: the roster, with the record you are inside expanded.

    Only the *active* artist expands. Listing every artist's songs would turn the nav
    into a second catalogue — one repository scan per roster entry, on every page — and
    a tree nobody can find anything in. So this is one read for the roster, plus two more
    for the branch you are standing on, and collapsed entries are a link and nothing else.

    `principal` is optional because `/verify` is deliberately public (see `verify_page`).
    A caller with no session gets the sections and an empty tree rather than an error:
    the alternative is either leaking a tenant's roster to anonymous visitors or dropping
    the sidebar on one page, and a nav that disappears is worse than a nav that is short.
    """
    if principal is None:
        return {"artists": [], "artist": None, "song": None, "songs": [], "kits": []}
    container = get_container()
    songs: list = []
    kits: list = []
    if artist is not None:
        songs = container.songs.list_for_artist(principal, artist.id)
        kits = container.kits.list(principal, artist_id=artist.id)
    return {
        "artists": container.artists.list(principal),
        "artist": artist,
        "song": song,
        "songs": songs,
        "kits": kits,
    }


def _page(request: Request, template: str, principal, *, nav_artist=None, nav_song=None, **ctx):
    """A full page: everything `_render` gives, plus the sidebar's tree.

    Separate from `_render` because the fragment routes vastly outnumber the page routes
    and a fragment has no sidebar to fill — building the tree for every htmx swap would
    put three repository reads behind every hook-window edit, for markup the response
    does not contain.
    """
    return _render(request, template, nav=_nav(principal, artist=nav_artist, song=nav_song), **ctx)


def _screen(request: Request, name: str, principal, *, nav_artist=None, nav_song=None, **ctx):
    """One screen, rendered as a page or as a layer depending on how it was asked for.

    The three secondary screens — pricing a kit, reading one, verifying an asset — are
    all things you do *about* something else that is on screen at the time. Sending
    someone to a different page and back for them is the round trip the layer removes.
    But each also has to survive being a URL: a bookmark, a link in Slack, a judge
    arriving at `/verify` with no account.

    So there is one route and two frames, chosen by where the caller said to put the
    answer: htmx sends `HX-Target` carrying the id of the element it will swap, and only
    a link asking to open a layer names `layer`. The `href` on every one of those links
    is the same URL, so with no JavaScript at all the link is an ordinary navigation to
    the full page.

    Keying on the target rather than on `HX-Request` matters here, because this screen
    has a second htmx caller: the generate form re-prices itself against this same
    handler, targeting `#plan-body`. On `HX-Request` alone a re-price from the full page
    would be answered with a dialog — it would happen to work, since `hx-select` pulls
    the same two regions out of either frame, but only by accident, and the first person
    to add a third caller would find out the hard way.

    The body is the same template either way (`components/_<name>_body.html`), for the
    reason every fragment in this console is shared: two definitions of what a kit costs
    would eventually disagree about it.
    """
    if request.headers.get("HX-Target") == "layer":
        return _render(request, f"overlays/{name}.html", overlay=True, **ctx)
    return _page(
        request, f"pages/{name}.html", principal,
        nav_artist=nav_artist, nav_song=nav_song, **ctx,
    )


def _optional(read):
    """Return what `read()` gives, or None if the record is simply not there.

    Used where a related record is decoration rather than the subject of the page — a
    kit whose song was deleted is still a kit with assets and a manifest worth showing.
    """
    try:
        return read()
    except ServiceError:
        return None


def _error_fragment(request: Request, exc: ServiceError, target: str = "") -> HTMLResponse:
    """Service refusals render as a component, not a stack trace.

    The rights refusal in particular is a thing the label needs to read and act on, so
    it gets the same visual treatment as any other panel — and a 422 status so htmx
    and scripts agree about what happened.
    """
    response = _render(request, "components/_error.html", message=str(exc), target=target)
    response.status_code = exc.status_code
    return response


# ---------------------------------------------------------------- pages
CONSOLE_PATH = "/console"


@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    """The public page — the marketing site, served by the same app as the console.

    No `Principal` in the signature, and that is the whole point: this is the one route
    that must render for someone who has never signed in and may never sign in. Every
    other page takes `CurrentPrincipal` and therefore raises `AuthError` without a
    session, which is what puts the console behind the login.

    It lives here rather than on a second host because `docs/web.md` costed that out:
    a separate front end is a second deploy target, a second dependency tree, and a
    $20/mo Vercel commercial seat — the entire always-on floor of this stack, spent on
    a page that never re-renders. One app, one deploy, per BUILD-SPEC §2.
    """
    return _render(request, "pages/landing.html")


@router.get(CONSOLE_PATH, response_class=HTMLResponse)
def roster(request: Request, principal: CurrentPrincipal, artists: Artists, kits: Kits):
    return _page(
        request,
        "pages/roster.html",
        principal,
        artists=artists.list(principal),
        kits=kits.list(principal)[:5],
    )


@router.get("/console/artists/{artist_id}", response_class=HTMLResponse)
def artist_detail(
    request: Request,
    artist_id: str,
    principal: CurrentPrincipal,
    artists: Artists,
    identities: Identities,
    songs: Songs,
    kits: Kits,
):
    try:
        artist = artists.get(principal, artist_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _page(
        request,
        "pages/artist.html",
        principal,
        nav_artist=artist,
        artist=artist,
        # The components this page includes address their own routes by `artist_id` —
        # that is their contract with the fragment handlers, which pass it explicitly.
        # Omitting it here rendered `/ui/artists//songs`, a form that 404s on submit.
        artist_id=artist.id,
        identity=identities.current(principal, artist_id),
        identities=identities.list_for_artist(principal, artist_id),
        songs=songs.list_for_artist(principal, artist_id),
        kits=kits.list(principal, artist_id=artist_id),
    )


@router.get("/verify", response_class=HTMLResponse)
def verify_page(request: Request, principal: MaybePrincipal):
    """Provenance checking, deliberately public — and therefore deliberately not under
    `/console`.

    PRODUCT.md keeps this when the rest of the ops/judge role is deferred, because a
    judge must be able to check a delivered asset without an account. It takes no
    `CurrentPrincipal`, which is exactly why it cannot live at `/console/verify`: every
    other path under that prefix requires a session, and a URL that implies a gate it
    does not have is worse than either choice made honestly.

    `MaybePrincipal` rather than nothing at all: the page stays public — an anonymous
    visitor resolves to `None` and gets a sidebar with no roster in it — while an
    operator who *is* signed in keeps their tree instead of watching it empty out for
    one screen. A judge and a label see two different navs and the same verifier.

    Safe to expose because it reads nothing: `verify_bytes` runs `manifest.verify()`
    over the uploaded bytes and touches no tenant document.
    """
    return _screen(request, "verify", principal, report=None)


@router.get("/console/catalogue", response_class=HTMLResponse)
def catalogue_page(
    request: Request,
    principal: CurrentPrincipal,
    catalogue: Catalogue,
    artists: Artists,
    artist_id: str = "",
):
    """What is missing, across collections — wireframe gap #2.

    The roster answers "who is on the label"; this answers "what is not yet usable",
    which is the question a release calendar actually generates.
    """
    return _page(
        request,
        "pages/catalogue.html",
        principal,
        catalogue=catalogue.gaps(principal, artist_id=artist_id or None),
        artists=artists.list(principal),
        artist_id=artist_id,
    )


@router.get("/console/songs/{song_id}", response_class=HTMLResponse)
def song_detail(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    artists: Artists,
    kits: Kits,
    recommendations: Recommendations,
    analysis: Analysis,
    transcription: Transcription,
):
    try:
        song = songs.get(principal, song_id)
        artist = artists.get(principal, song.artist_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return _page(
        request,
        "pages/song.html",
        principal,
        nav_artist=artist,
        nav_song=song,
        song=song,
        artist=artist,
        kits=[k for k in kits.list(principal) if k.song_id == song.id],
        report=recommendations.recommend(song),
        roles=list(SectionRole),
        analysis_available=analysis.available,
        analysis_reason=analysis.unavailable_reason,
        transcription_available=transcription.available,
        transcription_reason=transcription.unavailable_reason,
    )


@router.get("/console/kits/{kit_id}", response_class=HTMLResponse)
def kit_detail(
    request: Request,
    kit_id: str,
    principal: CurrentPrincipal,
    kits: Kits,
    songs: Songs,
    artists: Artists,
):
    try:
        kit = kits.get(principal, kit_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    # A kit outliving its song or artist is not an error worth a 500 — the kit and its
    # provenance are still the thing being looked at.
    song = _optional(lambda: songs.get(principal, kit.song_id))
    artist = _optional(lambda: artists.get(principal, kit.artist_id))
    return _screen(
        request, "kit", principal,
        nav_artist=artist, nav_song=song, kit=kit, song=song, artist=artist,
    )


@router.get("/console/artists/{artist_id}/identity", response_class=HTMLResponse)
def identity_page(
    request: Request,
    artist_id: str,
    principal: CurrentPrincipal,
    artists: Artists,
    identities: Identities,
    line: str | None = None,
):
    """The identity builder as its own surface — wireframe gap #1.

    The domain model has carried `reference_frames` since it was written and nothing
    could create one, so an identity was text-only in practice. This is the screen that
    closes that.

    `line` selects which face is being edited. An artist is a roster entry, and a band is
    one entry with several faces in it; `None` means the primary line. An unknown line
    falls through to `current`'s own fallback rather than 404ing, because a stale link
    should land you on a person rather than on an error.
    """
    try:
        artist = artists.get(principal, artist_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    lines = identities.lines(principal, artist_id)
    identity = identities.current(principal, artist_id, line) or identities.current(
        principal, artist_id
    )
    return _page(
        request,
        "pages/identity.html",
        principal,
        nav_artist=artist,
        artist=artist,
        artist_id=artist.id,
        identity=identity,
        lines=lines,
        # The versions of the line on screen. History is per line because versions count
        # per line — showing the artist's whole stack here would list one member's
        # revisions under another member's name.
        identities=lines.get(identity.name.strip(), []) if identity else [],
    )


# Per-shot edits arrive as `prompt_0`, `model_2`, `seconds_1` — one flat namespace rather
# than a nested structure, because the re-price control is a plain GET form and the queue
# form has to carry the identical values back as hidden inputs. Anything that survives a
# round trip through both without a serialiser is worth preferring here.
_OVERRIDE_FIELD = re.compile(r"^(prompt|model|seconds|identity_lock)_(\d+)$")

# What the screen was showing when the form was submitted, echoed back beside each edit.
# Not values a person set — the server's own last answer, returned so this handler can tell
# an edit from a field it filled in itself. See `_shot_overrides`.
_ECHO_FIELD = re.compile(r"^(sent_prompt|position_key)_(\d+)$")


def _optional_int(raw: str) -> int | None:
    """An empty number field is "not set", not a parse error.

    A blank `<input type="number">` submits as the empty string, and an `int | None`
    parameter rejects that with a 422 — so the budget ceiling, whose whole design is that
    leaving it blank means "no ceiling", made the Re-price button fail the moment anybody
    pressed it without one. Declaring the parameter as text and coercing here is the only
    place that distinction can be drawn, because by the time FastAPI has validated there
    is no empty string left to interpret.

    A value that is present but not a number is also treated as unset rather than refused.
    The field is a spend guard: refusing to render the page because somebody typed "abc"
    into it would hide the estimate they were about to check.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return None


def _count_field(raw: str, *, high: int) -> tuple[int, str]:
    """A count being typed into, and the number it currently means.

    The same empty-string problem as `_optional_int`, arrived at from the other side. The
    shot count is not optional — no number means no shots — but now that the plan prices
    itself as the field is edited, a blank box is a state every edit passes *through*:
    clearing "3" to type "4" leaves the field empty for as long as it takes to press the
    next key, and an `int` parameter answers that with a 422. The estimate would simply
    stop moving, which is the failure mode live pricing exists to remove.

    Returns the clamped count and what the field should say. They differ only when the
    box is empty: echoing "0" back into a field somebody is mid-way through retyping
    moves their caret for them, so a blank box stays blank and prices nothing until it
    has a number in it. An out-of-range number is echoed as the clamp, because there the
    box and the plan below it would otherwise disagree about what is being bought.
    """
    if not (raw or "").strip():
        return 0, ""
    count = max(0, min(_optional_int(raw) or 0, high))
    return count, str(count)


def _shot_overrides(params: Any) -> dict[int, dict]:
    """Read `prompt_N` / `model_N` / `seconds_N` out of a form or query string.

    Unrecognised keys are ignored rather than rejected: this parses attacker-reachable
    input, and the fields it does accept are re-validated downstream by
    `briefs.apply_overrides` (which clamps a length and drops an index that no longer
    exists). A malformed index is not an error worth a 422 on a screen whose job is to
    show a price.

    **A prefilled field is not an edit.** The prompt box is rendered holding the composed
    prompt, because a preview you cannot correct is one that only tells you afterwards —
    but that made every submission carry a prompt, so every submission looked like a
    rewrite. The consequences were not cosmetic: the plan re-prices on every keystroke,
    and each round trip pinned the current text as an override, so the prompt stopped
    tracking the brief. Changing format left the old format's prompt on the wire under
    the new format's name and price, and the identity fragment — which the box shows and
    the composer prepends — accumulated one copy per round trip.

    So the box's rendered value rides back with it as `sent_prompt_N`, and a `prompt_N`
    equal to it is what the server itself put there: not an override, and the shot keeps
    planning from the brief. This is dirty-tracking done on the server, which is the only
    side that knows what it rendered; doing it in the browser would put a second opinion
    about the plan on the one screen whose argument is that there is only one.

    `position_key_N` rides along for `apply_overrides` — what the shot at that index was
    when the edit was made. Compared there rather than here because this function has the
    form and that one has the plan.
    """
    echoed: dict[int, dict[str, str]] = {}
    for key in params.keys():
        match = _ECHO_FIELD.match(key)
        if not match:
            continue
        field_name, raw_index = match.groups()
        value = params[key]
        if isinstance(value, str):
            echoed.setdefault(int(raw_index), {})[field_name] = value

    out: dict[int, dict] = {}
    for key in params.keys():
        match = _OVERRIDE_FIELD.match(key)
        if not match:
            continue
        field_name, raw_index = match.groups()
        value = params[key]
        if isinstance(value, str) and not value.strip():
            continue
        index = int(raw_index)
        echo = echoed.get(index, {}).get("sent_prompt")
        if (
            field_name == "prompt"
            and isinstance(value, str)
            and echo is not None
            and value.strip() == echo.strip()
        ):
            continue
        out.setdefault(index, {})[field_name] = value

    for index, edits in out.items():
        key = echoed.get(index, {}).get("position_key")
        if key:
            edits[POSITION_KEY] = key
    # An entry carrying only the echo is not an override, and storing one would write a
    # bare `{"index": 0}` into every brief.
    return {i: e for i, e in out.items() if any(f in e for f in OVERRIDABLE)}


@router.get("/console/artists/{artist_id}/songs/{song_id}/generate", response_class=HTMLResponse)
def generate_page(
    request: Request,
    artist_id: str,
    song_id: str,
    principal: CurrentPrincipal,
    artists: Artists,
    songs: Songs,
    identities: Identities,
    kits: Kits,
    container_recipes: Recipes,
    video_count: str = "3",
    section_ids: Annotated[list[str], Query()] = [],
    identity_names: Annotated[list[str], Query()] = [],
    faces_chosen: str = "",
    tts_text: str = "",
    budget_cents: str = "",
    recipe_slug: str = "",
    line: str = "",
):
    """Cost *before* the button — wireframe gap #3.

    Everywhere else in the console the price of a kit appears after it has been bought.
    This screen prices the exact shot plan that the queue button would submit, by
    building the same plan `KitService.request` builds and running it through the same
    `estimate_cents`. Two code paths that disagree about what a kit costs would be worse
    than no estimate, so there is only one.

    """
    try:
        artist = artists.get(principal, artist_id)
        song = songs.get(principal, song_id)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    count, count_field = _count_field(video_count, high=8)
    # Same filter the kit service applies, for the same reason: a section deleted between
    # loading this page and pricing it must not show up in the plan as a window.
    chosen = [sid for sid in section_ids if song.section(sid)]
    # The formats this tenant can generate, and the one selected. Seeded on first read,
    # so a fresh tenant lands on a page with four choices rather than an empty picker.
    recipe_list = container_recipes.list(principal)
    chosen_recipe = next(
        (r for r in recipe_list if r.slug == (recipe_slug or DEFAULT_SLUG)), recipe_list[0]
    )

    ceiling = _optional_int(budget_cents)
    overrides = _shot_overrides(request.query_params)
    # `None` and `[]` are different answers — not asked means the primary line, an empty
    # tick-list means a kit with nobody in it — and a checkbox group sends nothing in
    # either case. `faces_chosen` is the marker that tells them apart. It cannot be the
    # empty string in `identity_names`, because that is the primary line's real name.
    faces = list(identity_names) if faces_chosen else None
    # The faces this plan is actually for, resolved the same way the plan resolves them.
    #
    # The screen used to describe `identities.current(...)` — the artist's newest primary
    # face — under a heading that read like a statement about the run. So a kit with every
    # box cleared showed "Identity v1 · 5 frames · face conditioned" in the pagebar while
    # every shot below it said "no identity — this shot will not hold a face", and the
    # louder of the two was the one that was wrong.
    plan_identities = identities.select(principal, artist.id, faces)

    plan, shots = kits.plan(
        principal,
        song_id=song_id,
        video_count=count,
        section_ids=chosen,
        overrides=overrides,
        identity_names=faces,
        tts_text=tts_text or None,
        recipe_slug=recipe_slug or DEFAULT_SLUG,
        line=line,
    )

    return _screen(
        request,
        "generate",
        principal,
        nav_artist=artist,
        nav_song=song,
        artist=artist,
        song=song,
        # No `identity`. This screen asked for the artist's newest primary face and then
        # described it as though it were a fact about the run — which it stops being the
        # moment the picker is touched. `plan_identities` is the same question asked of the
        # plan, and it is the only one the screen has any business answering.
        plan_identities=plan_identities,
        shots=shots,
        plan=plan,
        overrides=overrides,
        video_count=count,
        count_field=count_field,
        section_ids=chosen,
        windows=hook_windows(song, chosen),
        estimate_cents=plan.estimate_cents,
        blocked=artist.consent.blocks_generation,
        lines=identities.lines(principal, artist_id),
        recipes=recipe_list,
        recipe=chosen_recipe,
        line=line,
        identity_names=faces,
        tts_text=tts_text,
        budget_cents=ceiling,
        over_budget=ceiling is not None and plan.estimate_cents > ceiling,
    )


@router.get("/console/settings", response_class=HTMLResponse)
def settings_page(request: Request, principal: CurrentPrincipal):
    """Deployment — which backends are live, and what each one would need to be real.

    The footer already names them; this says what the gap is, so "why is generation
    mocked" is answerable without reading `deps.describe()`.
    """
    container = get_container()
    settings = container.settings
    return _page(
        request,
        "pages/settings.html",
        principal,
        described=container.describe(),
        axes=[
            ("Storage", "RK_STORAGE_BACKEND", settings.storage_backend, "b2",
             "Backblaze B2 bucket, key id, and application key."),
            ("Generation", "RK_GENERATOR_BACKEND", settings.generator_backend, "genblaze",
             "A credentialed provider — GMI Cloud, or Google via GCP_PROJECT."),
            ("Queue", "RK_QUEUE_BACKEND", settings.queue_backend, "sqs",
             "An SQS queue URL, and Batch to run the generator."),
            ("Mail", "RK_MAIL_BACKEND", settings.mail_backend, "resend",
             "A Resend API key and a sender domain verified in Resend."),
            ("Sign-in", "RK_AUTH_BACKEND", settings.auth_backend, "otp",
             "A session secret and an allowlist — no external service needed."),
            # The only axis whose "real" value is a package rather than a credential, and
            # the only one with no mock: see adapters/audio_unavailable.py.
            ("Analysis", "RK_ANALYSIS_BACKEND", container.analyzer.name, "numpy",
             "numpy (pip install -e '.[audio]') and ffmpeg on PATH for anything but a WAV."),
            # Whether a kit's loops will carry the record or whatever the model composed.
            # It belongs on this page for the same reason analysis does: the gap is a
            # missing binary rather than a missing credential, and it is invisible from the
            # output — a clip scored by the model plays perfectly well.
            ("Scoring", "RK_SCORE_WITH_MASTER", _scoring_state(container), "on",
             "ffmpeg on PATH, and an uploaded master on the song — the record is laid "
             "under every kit clip."),
        ],
    )


def _scoring_state(container) -> str:
    """`on` only when this process can actually do it, not when it intends to."""
    if not getattr(container.scoring, "enabled", False):
        return "off"
    return "on" if scoring_mux.available() else "no ffmpeg"


# ---------------------------------------------------------------- sign in
LOGIN_PATH = "/auth/login"


def _safe_next(raw: str) -> str:
    """Only same-origin paths survive.

    `?next=` is attacker-controllable — it arrives in a link someone can send. Without
    this, `?next=https://evil.example` turns the label's own login page into an open
    redirect, which is the classic way a phishing link gets a legitimate domain in front
    of it. A value must start with a single `/` to be used; everything else becomes the
    roster.
    """
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return CONSOLE_PATH


@router.get(LOGIN_PATH, response_class=HTMLResponse)
def login_page(request: Request, principal: MaybePrincipal, next: str = CONSOLE_PATH):
    """The card itself — and a redirect if the visitor already has a session.

    One of the two pages that render without one (the other is the landing page); every
    console route requires `CurrentPrincipal` and so refuses.
    """
    destination = _safe_next(next)
    if principal is not None and principal.authenticated:
        return RedirectResponse(destination, status_code=303)
    return _render(request, "pages/login.html", next=destination, email="", message=None)


@router.post("/ui/auth/request-code", response_class=HTMLResponse)
def ui_request_code(
    request: Request,
    accounts: Accounts,
    email: str = Form(...),
    next: str = Form(CONSOLE_PATH),
):
    try:
        sent = accounts.request_code(email)
    except ServiceError as exc:
        # The address form comes back with the message on it, rather than the generic
        # error fragment — which would swap the card's only form out and leave a
        # mistyped address needing a page reload to fix.
        response = _render(
            request,
            "components/_email_form.html",
            email=email.strip(),
            next=_safe_next(next),
            message=str(exc),
        )
        response.status_code = exc.status_code
        return response
    return _render(
        request,
        "components/_otp_form.html",
        email=sent.email,
        next=_safe_next(next),
        delivered=sent.delivered,
        dev_code=sent.dev_code,
        message=None,
    )


@router.post("/ui/auth/verify-code")
def ui_verify_code(
    request: Request,
    accounts: Accounts,
    email: str = Form(...),
    code: str = Form(...),
    next: str = Form(CONSOLE_PATH),
):
    try:
        account, token = accounts.verify_code(email, code)
    except ServiceError as exc:
        # The form comes back rather than a bare error, so a mistyped digit costs one
        # retype rather than a whole round trip through the email.
        response = _render(
            request,
            "components/_otp_form.html",
            email=email.strip().lower(),
            next=_safe_next(next),
            delivered=True,
            dev_code=None,
            message=str(exc),
        )
        response.status_code = exc.status_code
        return response

    # 204 + HX-Redirect rather than a 303: htmx issued this request and would otherwise
    # swap the roster page into the login card. The browser does the navigation.
    response = Response(status_code=204, headers={"HX-Redirect": _safe_next(next)})
    _set_session_cookie(response, token)
    log.info("signed in: %s", account.email)
    return response


@router.get("/auth/logout")
def logout(request: Request):
    response = RedirectResponse(LOGIN_PATH, status_code=303)
    container = get_container()
    # Sessions are stateless, so this is the only revocation there is: drop the browser's
    # copy. The token stays valid until it expires, which is why the TTL is a week and
    # not a year — see services/accounts.py.
    response.delete_cookie(
        container.settings.session_cookie, path="/", samesite="lax", httponly=True
    )
    return response


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_container().settings
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=settings.session_ttl_s,
        path="/",
        # httpOnly: console JavaScript never needs the token, so an XSS should not be
        # able to read it. SameSite=Lax: the console is all same-origin GETs and htmx
        # POSTs, so nothing legitimate is cross-site, and Lax blocks the CSRF shape
        # where another origin POSTs a form at a signed-in browser.
        httponly=True,
        secure=settings.session_cookie_secure and settings.env != "dev",
        samesite="lax",
    )


# ---------------------------------------------------------------- artist fragments
@router.post("/ui/artists", response_class=HTMLResponse)
def ui_create_artist(
    request: Request,
    principal: CurrentPrincipal,
    artists: Artists,
    name: str = Form(...),
    bio: str = Form(""),
    spotify: str = Form(""),
    instagram: str = Form(""),
):
    try:
        artists.create(
            principal, name=name, bio=bio, links={"spotify": spotify, "instagram": instagram}
        )
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(
        _render(request, "components/_roster.html", artists=artists.list(principal)),
        events.ARTISTS,
    )


@router.delete("/ui/artists/{artist_id}", response_class=HTMLResponse)
def ui_delete_artist(
    request: Request,
    artist_id: str,
    principal: CurrentPrincipal,
    artists: Artists,
    cascade: bool = False,
):
    """Refuses if songs, identities or kits still point here, naming what is in the way.

    The card asks first (`/ui/artists/{id}/dependents`) so the confirm prompt can say
    "3 songs, 1 kit" rather than a generic warning, and only then offers cascade.
    """
    try:
        artists.delete(principal, artist_id, cascade=cascade)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    # All four collections, because a cascade takes the artist's songs, identities and
    # kits with them — and announcing only the roster would leave the nav tree holding a
    # branch of records that no longer resolve.
    return announce(
        _render(request, "components/_roster.html", artists=artists.list(principal)),
        events.ARTISTS,
        events.SONGS,
        events.KITS,
        events.IDENTITY,
    )


@router.post("/ui/artists/{artist_id}/consent", response_class=HTMLResponse)
def ui_set_consent(
    request: Request,
    artist_id: str,
    principal: CurrentPrincipal,
    artists: Artists,
    granted: str = Form(""),
    signed_by: str = Form(""),
    notes: str = Form(""),
):
    try:
        artist = artists.set_consent(
            principal,
            artist_id,
            granted=granted.lower() in ("1", "true", "on", "yes"),
            signed_by=signed_by,
            notes=notes,
        )
    except ServiceError as exc:
        return _error_fragment(request, exc)
    # Consent is an artist-level fact that four other regions draw: the nav dot, the
    # roster card, this page's Consent stat, and the catalogue's blocked count.
    return announce(
        _render(request, "components/_consent.html", artist=artist), events.ARTISTS
    )


@router.post("/ui/artists/{artist_id}/identity", response_class=HTMLResponse)
def ui_save_identity(
    request: Request,
    artist_id: str,
    principal: CurrentPrincipal,
    identities: Identities,
    structural_features: str = Form(""),
    wardrobe: str = Form(""),
    negatives: str = Form(""),
    presentation: str = Form(""),
    build: str = Form(""),
    height: str = Form(""),
    name: str = Form(""),
    redirect: str = Form(""),
):
    def split(raw: str) -> list[str]:
        return [p.strip() for p in raw.split(",") if p.strip()]

    def enum_or_none(enum, raw: str):
        """An unrecognised value is treated as "not sent", not as an error.

        These arrive from radio groups whose values are the enum members, so a bad one
        means a stale page or a hand-built request rather than a person making a choice
        the form offered. Falling back to the inherited value is the behaviour that keeps
        an old open tab from silently wiping a silhouette somebody set since.
        """
        try:
            return enum(raw) if raw else None
        except ValueError:
            return None

    try:
        identity = identities.create_version(
            principal,
            artist_id,
            structural_features=structural_features or None,
            wardrobe=split(wardrobe),
            negatives=split(negatives),
            presentation=enum_or_none(Presentation, presentation),
            build=enum_or_none(BodyBuild, build),
            height=enum_or_none(HeightBand, height),
            name=name,
        )
    except ServiceError as exc:
        return _error_fragment(request, exc)

    if redirect:
        # Creating a line should land on it. Swapping the form in place would close the
        # modal over a page still showing whoever was being edited before.
        response = HTMLResponse("")
        response.headers["HX-Redirect"] = (
            f"/console/artists/{artist_id}/identity?line={quote(identity.name)}"
        )
        return response

    return announce(
        _render(
            request,
            "components/_identity.html",
            identity=identity,
            identities=identities.list_for_artist(principal, artist_id),
            artist_id=artist_id,
        ),
        events.IDENTITY,
    )


# ---------------------------------------------------------------- song fragments
@router.post("/ui/artists/{artist_id}/songs", response_class=HTMLResponse)
def ui_create_song(
    request: Request,
    artist_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    title: str = Form(...),
    bpm: str = Form(""),
    bpm_method: str = Form(""),
    spotify_url: str = Form(""),
):
    try:
        songs.create(
            principal,
            artist_id,
            title=title,
            bpm=float(bpm) if bpm.strip() else None,
            bpm_method=bpm_method,
            spotify_url=spotify_url,
        )
    except ValueError:
        return _error_fragment(request, ServiceError("BPM must be a number."))
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(
        _render(
            request,
            "components/_songs.html",
            songs=songs.list_for_artist(principal, artist_id),
            artist_id=artist_id,
        ),
        events.SONGS,
    )


@router.post("/ui/songs/{song_id}/hook", response_class=HTMLResponse)
def ui_set_hook(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    start_ms: int = Form(...),
    end_ms: int = Form(...),
):
    try:
        song = songs.set_hook(principal, song_id, start_ms=start_ms, end_ms=end_ms)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(
        _render(request, "components/_song_row.html", song=song, artist_id=song.artist_id),
        events.SONG,
    )


@router.post("/ui/identities/{identity_id}/frames", response_class=HTMLResponse)
async def ui_add_reference_frame(
    request: Request,
    identity_id: str,
    principal: CurrentPrincipal,
    identities: Identities,
    file: UploadFile = File(...),
    lighting: str = Form("neutral"),
    angle: str = Form(FrameAngle.FRONT.value),
    caption: str = Form(""),
):
    """Upload a reference still — the surface the domain model has always lacked.

    `ReferenceFrame` has been on `Identity` since it was written and nothing could
    create one, so an identity was text-only in practice. That is wireframe gap #1.

    Unlike a master, these bytes *do* pass through the application. A master is large
    and presigned browser-to-bucket; a reference still is a photograph of a few hundred
    kilobytes, and routing it through here means the key layout and the tenant check
    stay in one place rather than being re-implemented in JavaScript.
    """
    container = get_container()
    data = await file.read()
    if not data:
        return _error_fragment(request, Conflict("That file was empty."))

    try:
        identity = identities.get(principal, identity_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)

    # Same HIERARCHICAL shape as every other object: prefix / tenant / collection.
    #
    # The full digest is kept on the frame even though only its first sixteen characters
    # name the object. A frame handed to a provider as a conditioning image travels into
    # the step cache key and the manifest's canonical hash, and Genblaze falls back to
    # hashing the *URL* when the asset carries no hash of its own — which on B2 is
    # presigned and rotates, so an unhashed plate means a manifest that changes on every
    # render of a kit nobody edited.
    digest = hashlib.sha256(data).hexdigest()
    content_type = file.content_type or "image/png"
    suffix = Path(file.filename or "frame.png").suffix or ".png"
    key = (
        f"{container.settings.key_prefix}/tenants/{principal.tenant_id}"
        f"/identities/{identity.id}/frames/{digest[:16]}{suffix}"
    )
    container.storage.put(key, data, content_type=content_type)

    try:
        classified = FrameAngle(angle)
    except ValueError:
        # Front is what every frame uploaded before the classes existed actually is, so
        # it is the honest fallback for an unrecognised value rather than a refusal.
        classified = FrameAngle.FRONT

    frame = ReferenceFrame(
        key=key,
        lighting=lighting or "neutral",
        angle=classified,
        caption=caption or None,
        sha256=digest,
        content_type=content_type,
    )
    try:
        identity = identities.add_reference_frame(principal, identity_id, frame)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(
        _render(request, "components/_frames.html", identity=identity), events.IDENTITY
    )


@router.post("/ui/songs/{song_id}/measurement", response_class=HTMLResponse)
def ui_set_measurement(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    bpm: float = Form(...),
    bpm_method: str = Form(""),
    drop_ms: str = Form(""),
):
    try:
        song = songs.set_measurement(
            principal,
            song_id,
            bpm=bpm,
            bpm_method=bpm_method or None,
            # An empty field means "leave it alone", not "set it to zero" — passing 0
            # would silently claim the drop is at the very start of the track.
            drop_ms=int(drop_ms) if drop_ms.strip() else None,
        )
    except ValueError:
        return _error_fragment(request, Conflict("Drop must be a whole number of milliseconds."))
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(
        _render(request, "components/_measurement.html", song=song), events.SONG
    )


@router.post("/ui/songs/{song_id}/hook-window", response_class=HTMLResponse)
def ui_set_hook_window(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    start_ms: int = Form(...),
    end_ms: int = Form(...),
):
    """The song page's hook form.

    Separate from `/ui/songs/{id}/hook` only because the two callers want different
    markup back: the artist page swaps a whole song row, this swaps the window panel.
    Same service call, so they cannot disagree about what was saved.
    """
    try:
        song = songs.set_hook(principal, song_id, start_ms=start_ms, end_ms=end_ms)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(_render(request, "components/_hook.html", song=song), events.SONG)


# ---------------------------------------------------------------- section fragments
def _hooks_fragment(request: Request, song, recommendations: Recommendations) -> HTMLResponse:
    """The sections list and the recommendations, as one swap.

    They are rendered together because they are one thought: every recommendation is about
    a section, and marking, renaming or re-pointing one changes both panels. Two targets
    would mean two round trips and, between them, a screen showing a ranking of a list it
    no longer matches.
    """
    return _render(
        request,
        "components/_hooks.html",
        song=song,
        report=recommendations.recommend(song),
        roles=list(SectionRole),
    )


@router.get("/ui/songs/{song_id}/hooks", response_class=HTMLResponse)
def ui_hooks(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    recommendations: Recommendations,
):
    """Re-read the panel. The analysis poll fires this when a measurement lands."""
    try:
        song = songs.get(principal, song_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return _hooks_fragment(request, song, recommendations)


@router.post("/ui/songs/{song_id}/sections", response_class=HTMLResponse)
def ui_add_section(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    recommendations: Recommendations,
    start_ms: int = Form(...),
    end_ms: int = Form(...),
    role: str = Form("hook"),
    label: str = Form(""),
):
    try:
        song = songs.add_section(
            principal, song_id, start_ms=start_ms, end_ms=end_ms, role=role, label=label
        )
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(_hooks_fragment(request, song, recommendations), events.SONG)


@router.post("/ui/songs/{song_id}/sections/{section_id}", response_class=HTMLResponse)
def ui_update_section(
    request: Request,
    song_id: str,
    section_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    recommendations: Recommendations,
    start_ms: int = Form(...),
    end_ms: int = Form(...),
    role: str = Form(""),
    label: str = Form(""),
):
    try:
        song = songs.update_section(
            principal,
            song_id,
            section_id,
            start_ms=start_ms,
            end_ms=end_ms,
            role=role or None,
            label=label,
        )
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(_hooks_fragment(request, song, recommendations), events.SONG)


@router.delete("/ui/songs/{song_id}/sections/{section_id}", response_class=HTMLResponse)
def ui_delete_section(
    request: Request,
    song_id: str,
    section_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    recommendations: Recommendations,
):
    try:
        song = songs.remove_section(principal, song_id, section_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(_hooks_fragment(request, song, recommendations), events.SONG)


@router.post("/ui/songs/{song_id}/sections/{section_id}/primary", response_class=HTMLResponse)
def ui_set_primary_section(
    request: Request,
    song_id: str,
    section_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    recommendations: Recommendations,
    start_ms: str = Form(""),
    end_ms: str = Form(""),
):
    """Make this section the hook a kit cuts to — optionally at a recommended window.

    The two are one button on purpose. "Apply the suggestion" and "use this hook" arriving
    as separate clicks is how a person ends up with the hook pointed at a section whose
    window they meant to snap first, and the console cannot tell the difference afterwards.
    """
    try:
        if start_ms.strip() and end_ms.strip():
            songs.update_section(
                principal,
                song_id,
                section_id,
                start_ms=int(start_ms),
                end_ms=int(end_ms),
            )
        song = songs.set_primary_section(principal, song_id, section_id)
    except ValueError:
        return _error_fragment(request, Conflict("A window is a whole number of milliseconds."))
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(_hooks_fragment(request, song, recommendations), events.SONG)


# ---------------------------------------------------------------- master + analysis
@router.post("/ui/songs/{song_id}/master/upload-url")
def ui_master_upload_url(
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    content_type: str = Form(...),
):
    """Step one of the upload: where the browser should PUT the bytes.

    JSON rather than a fragment, and the one console route that is not htmx — a presigned
    upload is a request the *browser* makes to the bucket, so something has to hand it the
    URL. §2b rule 2 is the reason it is worth the small amount of JavaScript: a master is
    tens or hundreds of megabytes and must never pass through this process.
    """
    return songs.master_upload_url(principal, song_id, content_type=content_type)


@router.post("/ui/songs/{song_id}/master", response_class=HTMLResponse)
def ui_complete_master(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    analysis: Analysis,
    key: str = Form(...),
    content_type: str = Form(""),
):
    """Step two: the bytes have landed, so record them and start measuring.

    Analysis is queued here rather than behind a second button because an uploaded master
    that nobody measured is the state this whole feature exists to remove. A process that
    cannot analyse still records the master — the upload was real — and the panel says
    what is missing instead of failing the upload for it.
    """
    try:
        song = songs.register_master(principal, song_id, key=key, content_type=content_type)
    except ServiceError as exc:
        return _error_fragment(request, exc)

    note = ""
    try:
        song = analysis.request(principal, song.id)
    except ServiceError as exc:
        note = str(exc)
    return announce(
        _render(
            request,
            "components/_master.html",
            song=song,
            analysis_available=analysis.available,
            analysis_reason=analysis.unavailable_reason,
            note=note,
        ),
        events.SONG,
    )


@router.post("/ui/songs/{song_id}/analysis", response_class=HTMLResponse)
def ui_request_analysis(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    analysis: Analysis,
):
    try:
        song = analysis.request(principal, song_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(
        _render(
            request,
            "components/_analysis.html",
            song=song,
            analysis_available=analysis.available,
            analysis_reason=analysis.unavailable_reason,
        ),
        events.SONG,
    )


@router.get("/ui/songs/{song_id}/analysis", response_class=HTMLResponse)
def ui_analysis_panel(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    analysis: Analysis,
):
    """The htmx poll target while a measurement is running.

    Polling for the same reason kit rows do (§2b defers websockets), and on the same
    3-second cadence. When the job reaches a terminal state this fires `rk:analysis-done`
    so the hooks panel re-reads itself — the sections it is showing were just replaced.
    """
    try:
        song = songs.get(principal, song_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    response = _render(
        request,
        "components/_analysis.html",
        song=song,
        analysis_available=analysis.available,
        analysis_reason=analysis.unavailable_reason,
    )
    settled = song.analysis is None or song.analysis.status in (
        AnalysisStatus.DONE,
        AnalysisStatus.FAILED,
    )
    if settled:
        # Two audiences, one header. `rk:analysis-done` is the narrow one the hooks and
        # arrangement panels have always listened for; `rk:song` is the broad one that
        # repaints the header's Master and Cuts-to stats and the tab counts, which a
        # landing measurement changes just as much and which nothing was telling.
        response.headers["HX-Trigger"] = "rk:analysis-done"
        announce(response, events.SONG)
    return response


@router.get("/ui/songs/{song_id}/arrangement", response_class=HTMLResponse)
def ui_arrangement(request: Request, song_id: str, principal: CurrentPrincipal, songs: Songs):
    """The picture of the song, re-read when a measurement lands.

    Its own fragment rather than part of the analysis panel because the two live in
    different columns and are different widths — the analysis is a sidebar summary, the
    arrangement needs the full page to be legible at all. It listens for `rk:analysis-done`,
    which the analysis poll fires when the job reaches a terminal state, so a measurement
    finishing repaints the chart without the page being reloaded.
    """
    try:
        song = songs.get(principal, song_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return _render(request, "components/_arrangement.html", song=song)


# ---------------------------------------------------------------- lyric fragments
def _lyrics_panel(
    request: Request, song, transcription: Transcription, note: str = ""
) -> HTMLResponse:
    """The rail panel: what heard this song, and how far along it is."""
    return _render(
        request,
        "components/_lyrics.html",
        song=song,
        transcription_available=transcription.available,
        transcription_reason=transcription.unavailable_reason,
        note=note,
    )


@router.post("/ui/songs/{song_id}/transcription", response_class=HTMLResponse)
def ui_request_transcription(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    transcription: Transcription,
    force: str = Form(""),
):
    """Queue a transcription.

    `force` arrives as a form field rather than being inferred from "there are edits, they
    must have meant it": the refusal exists precisely because somebody clicking a button
    labelled "Re-transcribe" has usually forgotten what it costs, and a flag the console
    sets on their behalf is not a decision they made.
    """
    try:
        song = transcription.request(principal, song_id, force=bool(force.strip()))
    except ServiceError as exc:
        # The panel comes back with the refusal on it rather than as a bare error card:
        # the "you would lose N corrections" message is only actionable next to the button
        # that would do it anyway.
        try:
            song = songs.get(principal, song_id)
        except ServiceError:
            return _error_fragment(request, exc)
        return _lyrics_panel(request, song, transcription, note=str(exc))
    return announce(_lyrics_panel(request, song, transcription), events.SONG)


@router.get("/ui/songs/{song_id}/transcription", response_class=HTMLResponse)
def ui_transcription_panel(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    transcription: Transcription,
):
    """The htmx poll target while a transcription is running.

    Same 3-second cadence as the analysis panel and the kit rows, for the same reason
    (§2b defers websockets). When the job reaches a terminal state this fires
    `rk:lyrics-done` so the lyric pane re-reads itself — the lines it is showing were
    just replaced.
    """
    try:
        song = songs.get(principal, song_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    response = _lyrics_panel(request, song, transcription)
    settled = song.lyrics is None or song.lyrics.status in (
        AnalysisStatus.DONE,
        AnalysisStatus.FAILED,
    )
    if settled:
        # As with the analysis panel: the narrow event repaints the lyric pane, the broad
        # one the Lyrics tab count — which is the number of lines the model was unsure of,
        # and so the one thing on that tab worth interrupting somebody for.
        response.headers["HX-Trigger"] = "rk:lyrics-done"
        announce(response, events.SONG)
    return response


def _lyrics_fragment(request: Request, song) -> HTMLResponse:
    return _render(request, "components/_lyric_lines.html", song=song)


@router.get("/ui/songs/{song_id}/lyrics", response_class=HTMLResponse)
def ui_lyrics(request: Request, song_id: str, principal: CurrentPrincipal, songs: Songs):
    """Re-read the lyric pane. The transcription poll fires this when a run lands."""
    try:
        song = songs.get(principal, song_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return _lyrics_fragment(request, song)


@router.post("/ui/songs/{song_id}/lyrics/lines", response_class=HTMLResponse)
def ui_add_lyric_line(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    text: str = Form(...),
    start_ms: int = Form(0),
    end_ms: int = Form(0),
):
    try:
        song = songs.add_lyric_line(
            principal, song_id, text=text, start_ms=start_ms, end_ms=end_ms
        )
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(_lyrics_fragment(request, song), events.SONG)


@router.post("/ui/songs/{song_id}/lyrics/lines/{line_id}", response_class=HTMLResponse)
def ui_update_lyric_line(
    request: Request,
    song_id: str,
    line_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    text: str = Form(...),
    start_ms: int = Form(...),
    end_ms: int = Form(...),
):
    try:
        song = songs.update_lyric_line(
            principal, song_id, line_id, text=text, start_ms=start_ms, end_ms=end_ms
        )
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(_lyrics_fragment(request, song), events.SONG)


@router.delete("/ui/songs/{song_id}/lyrics/lines/{line_id}", response_class=HTMLResponse)
def ui_delete_lyric_line(
    request: Request, song_id: str, line_id: str, principal: CurrentPrincipal, songs: Songs
):
    try:
        song = songs.remove_lyric_line(principal, song_id, line_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(_lyrics_fragment(request, song), events.SONG)


@router.post("/ui/songs/{song_id}/lyrics/text", response_class=HTMLResponse)
def ui_set_lyrics_text(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    text: str = Form(""),
):
    """The whole lyric in one textarea — how a transcript actually gets fixed.

    Line-by-line editing is right for one wrong word and unusable for a chorus the model
    heard as gibberish, which is the common failure. `set_lyrics_text` keeps the windows
    that are already known and says so when the line count moves.
    """
    try:
        song = songs.set_lyrics_text(principal, song_id, text)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(_lyrics_fragment(request, song), events.SONG)


# ---------------------------------------------------------------- kit fragments
@router.post("/ui/kits", response_class=HTMLResponse)
async def ui_create_kit(
    request: Request,
    principal: CurrentPrincipal,
    kits: Kits,
    songs: Songs,
    song_id: str = Form(...),
    artist_id: str = Form(...),
    # Text, then coerced, for the reason `_count_field` gives: this button now submits the
    # priced form itself rather than a copy of it, so it receives that form's fields with
    # that form's blanks in them.
    video_count: str = Form("3"),
    section_ids: list[str] = Form(default=[]),
    identity_names: list[str] = Form(default=[]),
    faces_chosen: str = Form(""),
    tts_text: str = Form(""),
    budget_cents: str = Form(""),
    name: str = Form(""),
    recipe_slug: str = Form(""),
    line: str = Form(""),
):
    """Buy the plan that was on the screen — including every prompt somebody rewrote.

    The per-shot edits are read straight off the submitted form rather than declared as
    parameters, because their names carry the shot index (`prompt_0`, `model_2`) and the
    count is not known until the brief is planned. Reading the form is what keeps the
    queue button honest: whatever the preview priced is what this submits.
    """
    overrides = _shot_overrides(await request.form())
    try:
        kits.request(
            principal,
            song_id=song_id,
            video_count=_count_field(video_count, high=8)[0],
            section_ids=section_ids,
            overrides=overrides,
            identity_names=list(identity_names) if faces_chosen else None,
            tts_text=tts_text or None,
            budget_cents=_optional_int(budget_cents),
            name=name or None,
            recipe_slug=recipe_slug or DEFAULT_SLUG,
            line=line,
        )
    except ServiceError as exc:
        return _error_fragment(request, exc)
    # `songs` is what the panel's generate form builds its picker from. Rendering this
    # fragment without it did not fail — Jinja resolved the name to undefined, `{% if
    # songs %}` took the empty branch, and queueing a kit replaced the form with "Attach
    # a song before generating a kit" on a page listing the song it had just used.
    return announce(
        _render(
            request,
            "components/_kits.html",
            kits=kits.list(principal, artist_id=artist_id),
            songs=songs.list_for_artist(principal, artist_id),
            artist_id=artist_id,
        ),
        events.KITS,
    )


@router.get("/ui/kits/{kit_id}/row", response_class=HTMLResponse)
def ui_kit_row(request: Request, kit_id: str, principal: CurrentPrincipal, kits: Kits):
    """The htmx poll target while a kit is running.

    Polling rather than websockets on purpose: BUILD-SPEC §2b lists real-time
    websockets under deliberately deferred, and a kit takes minutes, so a 3-second
    poll on one row costs nothing and removes a whole class of infrastructure.

    A run *landing* is announced once, when the row comes back terminal — which is also
    the last time this route is asked for that kit, since the replacement markup carries
    no trigger. That is what repaints the nav's running dot, the Kits counts and the
    catalogue, none of which the row swap can reach. Announcing on every poll instead
    would re-render the whole page every three seconds for the length of the run.
    """
    try:
        kit = kits.get(principal, kit_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    response = _render(request, "components/_kit_row.html", kit=kit)
    if kit.status.value not in ("queued", "running"):
        announce(response, events.KITS)
    return response


@router.post("/ui/kits/{kit_id}/retry", response_class=HTMLResponse)
def ui_retry_kit(
    request: Request,
    kit_id: str,
    principal: CurrentPrincipal,
    kits: Kits,
    songs: Songs,
    artists: Artists,
):
    """Queue a failed kit again — from the row it failed in, or from its own screen.

    A failed run was a terminal screen: the error was rendered, and the only thing the
    console could do about it was delete the kit. Every retry meant rebuilding the brief
    by hand through the generate form, which for a kit carrying eight rewritten prompts
    is not a thing anybody reproduces exactly. The brief is stored on the kit, so the
    button just says "again".

    Three frames ask for this and each needs a different answer, keyed on `HX-Target`
    the way `_screen` keys on it:

    *   A **row** in a list gets the row back. It comes back `queued`, which means the
        replacement markup carries the 3-second poll again and the row converges on its
        own — the same mechanism that draws the first run.
    *   The **layer** gets the whole overlay re-rendered, because a kit opened over a
        list has no row of its own to swap.
    *   The **page** gets `HX-Refresh`, which is a plain reload of the screen you are
        already on. The kit page does not poll (it never has, for a first run either),
        so re-rendering it in place would show a queued kit that never moved.
    """
    try:
        kit = kits.retry(principal, kit_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)

    target = request.headers.get("HX-Target")
    if target == "layer":
        song = _optional(lambda: songs.get(principal, kit.song_id))
        artist = _optional(lambda: artists.get(principal, kit.artist_id))
        return announce(
            _render(
                request, "overlays/kit.html",
                overlay=True, kit=kit, song=song, artist=artist,
            ),
            events.KITS,
        )
    if target == f"kit-{kit.id}":
        return announce(_render(request, "components/_kit_row.html", kit=kit), events.KITS)

    # 204 rather than an empty body, for the reason `ui_set_identity_approval` gives:
    # a response with no target would otherwise be swapped into the button that sent it.
    response = Response(status_code=204)
    response.headers["HX-Refresh"] = "true"
    return announce(response, events.KITS)


@router.post("/ui/kits/{kit_id}/approval", response_class=HTMLResponse)
def ui_approve_kit(
    request: Request,
    kit_id: str,
    principal: CurrentPrincipal,
    kits: Kits,
    state: str = Form(...),
):
    try:
        kit = kits.set_approval(principal, kit_id, ApprovalState(state))
    except ValueError:
        # `ApprovalState(state)` raises `ValueError`, not `ServiceError`, so an unknown
        # state was an unhandled 500 here rather than a refusal the console could render.
        return _error_fragment(request, ServiceError(f"{state!r} is not an approval state."))
    except ServiceError as exc:
        return _error_fragment(request, exc)
    # Approval is the editorial axis, and it is counted a page away: the catalogue's "no
    # approved kit" gap is the label's daily question, and clearing one here used to
    # leave that number wrong until somebody reloaded.
    return announce(_render(request, "components/_kit_row.html", kit=kit), events.KITS)


# ---------------------------------------------------------------- destroy
@router.get("/ui/artists/{artist_id}/dependents", response_class=HTMLResponse)
def ui_artist_dependents(
    request: Request, artist_id: str, principal: CurrentPrincipal, artists: Artists
):
    """A one-line summary of what a cascade would take, for the confirm prompt."""
    try:
        counts = artists.dependents(principal, artist_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    detail = ", ".join(f"{n} {name}" for name, n in counts.items() if n)
    return HTMLResponse(detail or "nothing else")


@router.post("/ui/artists/{artist_id}", response_class=HTMLResponse)
def ui_update_artist(
    request: Request,
    artist_id: str,
    principal: CurrentPrincipal,
    artists: Artists,
    name: str = Form(...),
    bio: str = Form(""),
):
    try:
        artist = artists.update(principal, artist_id, name=name, bio=bio)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    # A rename is the change with the widest reach in the console: the nav tree, the
    # breadcrumb, the page heading and every catalogue row that names this artist.
    return announce(
        _render(request, "components/_artist_head.html", artist=artist), events.ARTISTS
    )


@router.post("/ui/songs/{song_id}", response_class=HTMLResponse)
def ui_update_song(
    request: Request,
    song_id: str,
    principal: CurrentPrincipal,
    songs: Songs,
    title: str = Form(...),
):
    """Title only — every other field on a song is measured and owns its `method`."""
    try:
        song = songs.update(principal, song_id, title=title)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(
        _render(request, "components/_song_head.html", song=song), events.SONGS
    )


@router.delete("/ui/songs/{song_id}", response_class=HTMLResponse)
def ui_delete_song(
    request: Request, song_id: str, principal: CurrentPrincipal, songs: Songs, kits: Kits
):
    """Refuses while kits still reference it — a kit whose song vanished cannot be
    re-planned, and its brief names section ids that no longer resolve."""
    try:
        song = songs.get(principal, song_id)
        existing = [k for k in kits.list(principal) if k.song_id == song_id]
        if existing:
            raise Conflict(
                f"{len(existing)} kit(s) were generated from {song.title!r}. Delete them first."
            )
        artist_id = song.artist_id
        songs.delete(principal, song_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    response = _render(
        request, "components/_songs.html", songs=songs.list_for_artist(principal, artist_id),
        artist_id=artist_id,
    )
    # The song page is gone, so a delete issued from it has nowhere to swap into.
    response.headers["HX-Redirect"] = f"/console/artists/{artist_id}"
    return response


@router.delete("/ui/identities/{identity_id}", response_class=HTMLResponse)
def ui_delete_identity(
    request: Request, identity_id: str, principal: CurrentPrincipal, identities: Identities
):
    try:
        identity = identities.get(principal, identity_id)
        artist_id = identity.artist_id
        identities.delete(principal, identity_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    response = HTMLResponse("")
    response.headers["HX-Redirect"] = f"/console/artists/{artist_id}/identity"
    return response


@router.delete("/ui/identities/{identity_id}/frames/{index}", response_class=HTMLResponse)
def ui_delete_reference_frame(
    request: Request,
    identity_id: str,
    index: int,
    principal: CurrentPrincipal,
    identities: Identities,
):
    """Remove one reference still, and the object behind it.

    The gap this closes is not cosmetic: frames could be added and never removed, so a
    mis-classified upload — a profile filed as a front — was permanent, and it kept
    winning the one conditioning slot for every kit afterwards.
    """
    try:
        identity = identities.remove_reference_frame(principal, identity_id, index)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    return announce(
        _render(request, "components/_frames.html", identity=identity), events.IDENTITY
    )


@router.post("/ui/identities/{identity_id}/restore", response_class=HTMLResponse)
def ui_restore_identity(
    request: Request, identity_id: str, principal: CurrentPrincipal, identities: Identities
):
    """Copy an older version forward as the new current one.

    A restore that rewound in place would leave a kit generated last week pointing at a
    version number whose content had since changed underneath it — the exact failure the
    versioning exists to prevent. History only grows, and going back is itself an event.
    """
    try:
        identity = identities.restore_version(principal, identity_id)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    response = HTMLResponse("")
    response.headers["HX-Redirect"] = f"/console/artists/{identity.artist_id}/identity"
    return response


@router.post("/ui/identities/{identity_id}/approval", response_class=HTMLResponse)
def ui_approve_identity(
    request: Request,
    identity_id: str,
    principal: CurrentPrincipal,
    identities: Identities,
    state: str = Form(...),
):
    """The editorial gate, on the record that decides what an artist looks like.

    `set_approval` has been on the service since it was written with nothing able to call
    it, so every identity in the console was permanently a draft.

    Answered with 204 and an announcement rather than the full-page `HX-Refresh` this
    used to send. The refresh worked, and it was the one control in the console that
    threw the whole screen away to change one badge — which on this page means losing the
    scroll position in a three-column workspace and re-reading every version in history
    to redraw a word. The regions that actually show approval say so themselves now.
    """
    try:
        identities.set_approval(principal, identity_id, ApprovalState(state))
    except ValueError:
        # 400 rather than 409: this is a malformed value, not a conflict with stored
        # state. Only reachable from a stale page or a hand-built request, since the
        # buttons that call it are rendered from the enum.
        return _error_fragment(request, ServiceError(f"{state!r} is not an approval state."))
    except ServiceError as exc:
        return _error_fragment(request, exc)
    # 204 rather than an empty body: these buttons carry no `hx-target`, so htmx would
    # swap the response into the button itself and leave it captionless. A 204 is the
    # documented "I did it, swap nothing" answer, and the header still fires.
    return announce(Response(status_code=204), events.IDENTITY)


@router.delete("/ui/kits/{kit_id}", response_class=HTMLResponse)
def ui_delete_kit(
    request: Request,
    kit_id: str,
    principal: CurrentPrincipal,
    kits: Kits,
    songs: Songs,
    force: bool = False,
):
    """Removes the kit and the assets and manifest it owns in the bucket.

    Refuses on an approved or still-running kit unless `force` — approval is the record
    that a human decided this was publishable, and something publishable may already be
    published.
    """
    try:
        kit = kits.get(principal, kit_id)
        artist_id = kit.artist_id
        kits.delete(principal, kit_id, force=force)
    except ServiceError as exc:
        return _error_fragment(request, exc)
    # The row's delete button targets `#kits`, which exists on the artist page and not on
    # the song page — where the same row is listed inside a pane of its own. There the
    # swap finds nothing and htmx drops it; the announcement is what repaints that list,
    # so deleting a kit from a song now works on both screens.
    return announce(
        _render(
            request,
            "components/_kits.html",
            kits=kits.list(principal, artist_id=artist_id),
            songs=songs.list_for_artist(principal, artist_id),
            artist_id=artist_id,
        ),
        events.KITS,
    )


# ---------------------------------------------------------------- verify fragment
@router.post("/ui/verify", response_class=HTMLResponse)
async def ui_verify(request: Request, verify: Verify, file: UploadFile = File(...)):
    data = await file.read()
    report = verify.verify_bytes(data, file.filename or "")
    return _render(request, "components/_verify_report.html", report=report, filename=file.filename)


# ---------------------------------------------------------------- local file serving
@router.put("/files/{key:path}")
async def receive_local_file(key: str, request: Request, principal: CurrentPrincipal):
    """The other half of `presign_put` when storage is a filesystem.

    On B2 the browser PUTs straight to the bucket and this route is never reached — which
    is the whole point of §2b rule 2. On a laptop `presign_put` degrades to `/files/…`, and
    without a handler behind it the console's upload succeeded in the browser and wrote
    nothing, which is the worst kind of working demo.

    It is bounded exactly as the GET is: local storage only, never under `tenants/`, and
    behind a principal. Masters land under `masters/<tenant>/…`, and the key is checked
    against *this* caller's tenant so a signed-in user cannot write into another one's
    prefix — the presigned URL is generated per tenant, but the route has to enforce that
    rather than assume the browser only ever sends back what it was given.
    """
    container = get_container()
    if container.settings.storage_backend != "local":
        raise HTTPException(status_code=404, detail="Not available on this storage backend")
    prefix = f"{container.settings.key_prefix}/masters/{principal.tenant_id}/"
    if not key.startswith(prefix):
        raise HTTPException(status_code=403, detail="That key is not writable")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload")
    container.storage.put(key, data, content_type=request.headers.get("content-type"))
    return Response(status_code=200)


_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _ranged(request: Request, data: bytes, media_type: str) -> Response:
    """The object, or the slice of it a media element asked for.

    Seeking is what makes a section playable. An `<audio>` told to start 124 seconds in
    will only jump there if the server answers ranges; without `Accept-Ranges` the browser
    downloads the whole master first, which on a 128 MB WAV is the difference between
    auditioning a hook and waiting for one.

    Only the single-range form is answered — a media element never sends more — and a
    header this does not parse falls through to the whole object rather than to a 416,
    because refusing a fetch over a header we chose not to understand is worse than
    serving it.
    """
    total = len(data)
    headers = {"accept-ranges": "bytes"}
    match = _RANGE.match(request.headers.get("range", "").strip())
    if not match or not total:
        return Response(content=data, media_type=media_type, headers=headers)

    first, last = match.group(1), match.group(2)
    if first == "":
        # `bytes=-N` — the final N bytes, which is how a player reads a trailing index.
        start, end = max(total - int(last or 0), 0), total - 1
    else:
        start = int(first)
        end = int(last) if last else total - 1
    end = min(end, total - 1)
    if start > end:
        return Response(
            status_code=416, headers={**headers, "content-range": f"bytes */{total}"}
        )
    headers["content-range"] = f"bytes {start}-{end}/{total}"
    return Response(
        content=data[start : end + 1], status_code=206, media_type=media_type, headers=headers
    )


@router.get("/files/{key:path}")
def serve_local_file(key: str, request: Request, principal: CurrentPrincipal):
    """Dev-only asset serving for `storage_backend=local`.

    On B2 the browser gets a presigned URL and this route is never reached — which is
    the whole point of rule 2. It exists so the console shows real generated assets on
    a laptop instead of broken images.

    It takes a `Principal` for the same reason every other route does, and here it is
    load-bearing rather than ceremonial: this handler reads an arbitrary object key out
    of the URL, so leaving it open would have made it the one way around the login —
    fetch the bytes directly and skip the page that would have asked who you are.

    Documents are refused, signed in or not. The YAML under `tenants/` is the repository's
    private storage — accounts, artists, kits — and nothing in the console ever links to
    it, so refusing it costs no legitimate fetch.

    The *prefix* used to be refused, though, and that claim was true when it was written
    and quietly stopped being true. Identity reference frames live under
    `tenants/{tenant}/identities/{id}/frames/`, and the identity page links to every one of
    them — so a laptop on local storage rendered an artist with five broken images while
    the bytes sat on disk. Nobody saw it on B2, where those frames are presigned and this
    route is never reached, which is exactly how a dev-only path rots.

    So the rule is what it always meant: not *this prefix*, but *documents, and other
    people's things*. A tenant's media is served to that tenant and nobody else — the
    handler takes an arbitrary key from the URL, so without the ownership check a signed-in
    user of one tenant could read another's photographs by typing the path. `runs/` and
    `masters/` are unchanged.
    """
    container = get_container()
    if container.settings.storage_backend != "local":
        raise HTTPException(status_code=404, detail="Not available on this storage backend")
    tenants_root = f"{container.settings.key_prefix}/tenants/"
    if key.startswith(tenants_root):
        own = f"{tenants_root}{principal.tenant_id}/"
        # A document is a document whoever asks; another tenant's media is not this
        # caller's to see. 404 rather than 403 for both, because a distinguishable refusal
        # tells an unauthorised caller which keys exist.
        if not key.startswith(own) or key.endswith(".yaml"):
            raise HTTPException(status_code=404, detail="No such object")
    try:
        data = container.storage.get(key)
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=404, detail="No such object") from exc

    suffix = key.rsplit(".", 1)[-1].lower()
    media_type = {
        "mp4": "video/mp4",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "json": "application/json",
        "flac": "audio/flac",
        "m4a": "audio/mp4",
        "yaml": "application/yaml",
    }.get(suffix, "application/octet-stream")
    return _ranged(request, data, media_type)
