"""Configuration — one object, env-driven, with defaults that run on a laptop.

The four `*_backend` fields are the whole deployment story. Each selects an adapter
behind a port; the defaults are the zero-credential path, and every production value
is additive rather than a rewrite:

    storage_backend    local  → b2
    generator_backend  mock   → genblaze
    queue_backend      inline → sqs
    auth_backend       none   → otp
    mail_backend       console → resend

`RK_` prefixes everything so these never collide with the provider keys Genblaze reads
directly from the environment (`GMI_API_KEY`, `B2_KEY_ID`, …).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


def _repo_env() -> Path:
    """The repository-root `.env`, found by walking up rather than by counting.

    This was `env_file=".env"` — a *relative* path, resolved against the working
    directory of whatever started the process. RemixKit therefore read
    `apps/remixkit/.env` when launched from its own directory and the root one when
    launched from the root, and nothing said which. The two files were merged into the
    root on 2026-08-15 and this names that file explicitly.

    Walking up to the directory holding `.git`, rather than taking `parents[3]`, because
    a fixed depth is precisely what the restructure earlier that day broke in three
    other places: `dev.sh` and `ccloud-mcp-setup.sh` computed the root as `../..` and
    silently found no `.env`, and two tests climbed into a directory that no longer
    existed. A search that fails loudly beats an offset that quietly points somewhere
    plausible.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists():
            return parent / ".env"
    # No repository above us — a wheel, a Lambda bundle, a stray copy. Fall back to the
    # working directory, which is what this did before, and let the field defaults carry
    # it if that is absent too.
    return Path(".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RK_", env_file=_repo_env(),
                                      extra="ignore")

    app_name: str = "RemixKit"
    env: Literal["dev", "staging", "prod"] = "dev"

    # ---- tenancy ---------------------------------------------------------------
    # The label. Tenant #1 dogfoods everything (PRODUCT.md); the product is generic.
    default_tenant_id: str = "respect-the-funk"
    default_tenant_name: str = "Respect the Funk"

    # ---- adapter selection -----------------------------------------------------
    storage_backend: Literal["local", "b2"] = "local"
    generator_backend: Literal["mock", "genblaze"] = "mock"
    queue_backend: Literal["inline", "sqs"] = "inline"
    auth_backend: Literal["none", "otp"] = "none"
    mail_backend: Literal["console", "resend"] = "console"
    # There is no `mock` here on purpose. Every other backend has a zero-credential adapter
    # that does the real thing badly; a fake measurement is a float that nothing downstream
    # could ever tell from a real one. `auto` uses numpy if it is installed and refuses
    # with the missing piece named if it is not — see adapters/audio_unavailable.py.
    analysis_backend: Literal["auto", "none"] = "auto"
    # And no `mock` here either, for a sharper version of the same reason. A fabricated
    # measurement is a wrong number; a fabricated lyric is words attributed to an artist,
    # rendered as the song's own and offered to the generate form. See
    # adapters/lyrics_unavailable.py.
    transcription_backend: Literal["auto", "none"] = "auto"

    # A deployment that declares itself authenticated must not be served by
    # AnonymousAuth. Startup fails loudly rather than quietly admitting everyone.
    require_auth: bool = False

    # ---- auth: email OTP -------------------------------------------------------
    # Who may sign in, comma-separated. This IS the user table — an allowlist rather
    # than open registration, because the console administers a real label's roster and
    # "anyone with an email address" is not the intended population. Growing past a
    # handful of people is the signal to make accounts self-registering behind an
    # invite, not to keep extending this string.
    allowed_emails: str = ""

    # Signs session tokens and hashes OTP codes. Empty is the laptop default: a random
    # per-process secret is generated, which logs everyone out on restart — correct for
    # dev, and `require_auth` refuses to let it happen in a deployment.
    session_secret: str = ""
    session_ttl_s: int = 7 * 24 * 3600
    session_cookie: str = "rk_session"
    # Cookies are Secure everywhere except dev, where there is no TLS on localhost.
    session_cookie_secure: bool = True

    otp_length: int = 6
    otp_ttl_s: int = 600
    # Five guesses against a six-digit code over ten minutes. Past this the challenge is
    # destroyed and the user must request a new one — which is also the rate limit on
    # brute force, since each new challenge is a new code.
    otp_max_attempts: int = 5
    # Floor between two code requests for the same address, so the sign-in form cannot
    # be used to mailbomb someone on the allowlist.
    otp_resend_interval_s: int = 30

    # ---- mail ------------------------------------------------------------------
    # Resend's API key. In a deployment it arrives from the shared SSM parameter
    # `/resend/api-key` (bootstrap.load_secrets, via RK_RESEND_KEY_PARAMETER); on a
    # laptop, RK_RESEND_API_KEY in `.env`.
    resend_api_key: str = ""
    # Must be on a domain verified in Resend, or the API refuses the send. Not agfarms.dev:
    # the Resend plan has no room for another domain, so the label sends from mattrickslauer.com.
    mail_from: str = "rtp@mattrickslauer.com"
    mail_from_name: str = "Respect the Funk"

    @property
    def allowlist(self) -> frozenset[str]:
        """The allowlist, normalised. Comma-separated rather than a JSON list because
        this arrives as one SSM parameter and one `.env` line, and both are edited by
        hand."""
        return frozenset(
            part.strip().lower() for part in self.allowed_emails.split(",") if part.strip()
        )

    # ---- storage ---------------------------------------------------------------
    local_storage_dir: Path = Path(".remixkit-data")
    b2_bucket: str = ""
    b2_key_id: str = ""
    b2_app_key: str = ""
    b2_region: str = ""
    key_prefix: str = "remixkit"

    # ---- generation ------------------------------------------------------------
    # Empty means "ask whichever provider resolved for the model it actually serves"
    # (`adapters/providers.DEFAULT_MODELS`). These were GMI slugs, which was correct only
    # while GMI was the only live provider: the provider is chosen at runtime from what is
    # keyed, so a hardcoded `seedance-…` was being handed to Sora or Veo the moment GMI
    # was unkeyed. Set one of these to pin a model, and it applies to whoever answers.
    video_model: str = ""
    image_model: str = ""
    audio_model: str = ""
    generation_timeout_s: float = 900.0
    max_concurrency: int = 4
    # Kits are capped because provider latency and quota are the named Layer-1 risk
    # (BUILD-SPEC §12). Small kits fail fast and cost little.
    max_shots_per_kit: int = 8
    # Hard ceiling on the estimated cost of a single run, in cents. 0 disables it.
    # This exists because Genblaze ships no spend guardrail of its own and a failed step
    # is still a billed step: on 2026-07-31 a four-model video fallback chain was
    # submitted from one call and charged for output it never produced. 500¢ is roughly
    # a full eight-shot kit on the priced models in `adapters/pricing`.
    max_run_cents: int = 500
    # Pin a vendor per modality: "gmicloud" | "openai" | "google" | "elevenlabs".
    # Empty is the preference order in `adapters/providers.VENDOR_ORDER`. These exist so
    # that "video goes to Sora, not seedance" is a deploy-time setting rather than a code
    # change — which is what it needs to be when a provider starts failing in production.
    # The payload key a model expects multiple reference images under, e.g.
    # "reference_images". Empty means one reference — the only count this code can claim
    # to have verified, because every GMI image model resolves through a fallback
    # declaring a single positional `image` slot and extras are silently dropped. Set this
    # only after checking the model's actual payload in GMI's catalogue: a wrong key is
    # either a 400 or, worse, a kit that reports four references and was conditioned on
    # one. See `adapters/generator_genblaze._reference_limit`.
    # The image-to-image model an identity-locking still is rendered with, e.g.
    # "seededit-3-0-i2i-250628". Empty means the face is not conditioned on a reference at
    # all, and the plan screen says so rather than a run failing.
    #
    # There is no default and there deliberately is not one. A hardcoded slug 404'd on a
    # real account (`InvalidEndpointOrModel.NotFound`) because the value came from the
    # connector's `example_slugs`, which its own docstring calls "documentation grade, not
    # a contract" — and GMI media providers declare `DiscoverySupport.PARTIAL`, so there is
    # no catalog endpoint to check a slug against. The only liveness test is the
    # empty-payload probe, which is a billable generation and is off.
    #
    # Which models an account can reach is therefore a fact only the operator has. See
    # `providers.REFERENCE_MODEL_CANDIDATES` for what to look for in the GMI console.
    image_edit_model: str = ""
    # The image-to-video model a locked clip runs on, e.g. "kling-o1-reference-to-video".
    # The default video model matters here for the same reason: GMI's hub tags
    # `seedance-2-0-260128` Text-to-Video only, so the still the lock paid to render is
    # discarded by the clip that was supposed to inherit it.
    video_edit_model: str = ""

    reference_slot: str = ""
    reference_max: int = 4

    video_provider: str = ""
    image_provider: str = ""
    audio_provider: str = ""

    # ---- scoring ---------------------------------------------------------------
    # Lay the uploaded master under every video a kit produces, cut to the hook the shot
    # was planned for (`services.scoring`). On by default because it is what a kit is for:
    # the loops are backdrops the record plays over, and a clip carrying a soundtrack the
    # model composed for itself is the 2026-07-31 defect rather than a bonus.
    #
    # Off is for a deployment whose worker has no ffmpeg, or for the rare kit that wants
    # the provider's own bytes untouched. It does not change what is generated — only what
    # is written beside it and what a download hands back.
    score_with_master: bool = True

    # ---- analysis --------------------------------------------------------------
    # How much of a master to measure. Ten minutes covers any single and bounds the cost
    # of a full-track comb fit on a file somebody uploaded by mistake.
    analysis_window_s: float = 600.0

    # ---- transcription ---------------------------------------------------------
    transcription_model: str = "scribe_v1"
    # ISO-639 code, or empty to let the provider detect it. Worth setting for a catalogue
    # that is all one language: detection is the weakest step on a processed vocal, and a
    # wrong detection is a whole transcript of plausible words in the wrong language.
    transcription_language: str = ""
    # A provider round trip on a whole master, not a local decode — so this is a network
    # ceiling, and it is generous because the job runs on Batch where nothing is waiting.
    transcription_timeout_s: float = 600.0
    # Where a line ends. Both are the adapter's grouping thresholds, hoisted here because
    # a catalogue of dense rap and one of sparse ballads want different answers and
    # neither is a code change. See adapters/lyrics_elevenlabs.py.
    transcription_gap_ms: int = 900
    transcription_line_chars: int = 42

    # ---- secrets ---------------------------------------------------------------
    # Where `bootstrap.load_secrets` fetches credentials from. Declared here rather than
    # read only from the process environment so that the repo-root `.env` can name it: an .env
    # that turns on `storage_backend=b2` but cannot say where the B2 keys live sets up a
    # failure whose message is about missing credentials rather than about the missing
    # setting that would have found them.
    ssm_path: str = ""
    # Vertex project/region for the Google (Veo/Imagen) provider. Settings rather than
    # bare environment variables so the repo-root `.env` can supply them — see `export_for_libs`.
    gcp_project: str = ""
    gcp_location: str = "us-central1"
    # Which AWS credential profile to use. Empty means boto3's own resolution (env vars,
    # then `default`), which is right on Lambda — the execution role has no profile.
    aws_profile: str = ""

    # ---- queue -----------------------------------------------------------------
    sqs_queue_url: str = ""
    aws_region: str = "us-east-1"
    # Which Batch job to start when a kit is enqueued. Without these the message lands
    # on SQS and nothing consumes it.
    batch_job_queue: str = ""
    batch_job_definition: str = ""

    @property
    def is_readonly_fs(self) -> bool:
        """Serverless filesystems are read-only apart from /tmp. The local storage
        adapter needs to know so it can relocate rather than crash on first write."""
        return self.env != "dev" and self.storage_backend == "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()
