"""Load secrets from SSM into the environment, before settings are read.

Terraform creates the parameters but deliberately never holds their values — putting a
B2 application key into Terraform state would store it in plaintext. So the values are
set out of band, and something has to fetch them at runtime. This is that something.

It must run **before** the first `get_settings()` call, because that is `lru_cache`d and
whatever the environment looked like at that moment is what the process believes
forever. Hence a plain function called at the top of `create_app()` and the worker's
`main()`, rather than a FastAPI dependency or a startup hook.

Names map straight across: the parameter `/remixkit/prod/B2_APP_KEY` becomes the
environment variable `RK_B2_APP_KEY`, so a value that arrives via SSM in production and
via `.env` on a laptop is read by the same settings field either way. Provider keys
that Genblaze reads directly from the environment (`GMI_API_KEY`, `ELEVENLABS_API_KEY`)
are additionally exported unprefixed, because those are read by the library, not by us.

One secret lives outside that path: the Resend API key is shared across projects at
`/resend/api-key`. When `RK_RESEND_KEY_PARAMETER` names it, it is fetched as well and
lands as `RK_RESEND_API_KEY` (and `RESEND_API_KEY`). A missing parameter is skipped with
a warning, like a placeholder; `mail_backend=resend` then refuses to start for want of
the key, which names the problem.

Failure is fatal on purpose. A process that cannot read its credentials should not come
up and serve errors; it should refuse to start, where a deployment notices.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# Keys Genblaze and its provider extras read directly from the environment. These get
# exported without the RK_ prefix as well as with it.
_PROVIDER_KEYS = {
    "GMI_API_KEY",
    "OPENAI_API_KEY",
    "ELEVENLABS_API_KEY",
    "GOOGLE_API_KEY",
    "B2_KEY_ID",
    "B2_APP_KEY",
}


def load_secrets(
    path: str | None = None,
    region: str | None = None,
    profile: str | None = None,
) -> int:
    """Fetch every parameter under `path` into `os.environ`. Returns how many landed.

    A no-op when no path is given and `RK_SSM_PATH` is unset, which is the whole
    local-development story: nothing to configure, nothing to stub.

    `path` is passed explicitly by `create_app`, from settings — so the repo-root `.env` can name
    it. Falling back to the environment keeps the deployed path unchanged, where the
    variable is set by Terraform and there is no .env at all.

    `profile` exists for the same reason: on a laptop the right credentials are usually
    a named profile, and boto3 would otherwise silently use `default` and fail with an
    authentication error that says nothing about which identity it tried.
    """
    path = path or os.environ.get("RK_SSM_PATH", "")
    resend_parameter = os.environ.get("RK_RESEND_KEY_PARAMETER", "")
    if not path and not resend_parameter:
        return 0

    region = region or os.environ.get("RK_AWS_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
    profile = profile or os.environ.get("AWS_PROFILE") or ""

    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise RuntimeError(
            "RK_SSM_PATH is set but boto3 is not installed: pip install 'remixkit[queue]'"
        ) from exc

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    client = session.client("ssm", region_name=region)
    loaded = 0

    if resend_parameter:
        loaded += _load_resend_key(client, resend_parameter)
    if not path:
        return loaded

    paginator = client.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=path, Recursive=True, WithDecryption=True):
        for parameter in page.get("Parameters", []):
            name = parameter["Name"].rsplit("/", 1)[-1]
            value = parameter.get("Value", "")

            # Terraform seeds every parameter with a placeholder. Treating that as a
            # real credential would produce a confusing auth failure from the provider
            # instead of an obvious configuration error here.
            if not value or value.startswith("PLACEHOLDER"):
                log.warning("SSM parameter %s is unset — skipping", parameter["Name"])
                continue

            os.environ.setdefault(f"RK_{name}", value)
            if name in _PROVIDER_KEYS:
                os.environ.setdefault(name, value)
            loaded += 1

    log.info("loaded %d secret(s) from SSM path %s", loaded, path)
    return loaded


def _load_resend_key(client, name: str) -> int:
    """The one parameter read by name rather than by path. Returns 1 if it landed."""
    try:
        value = client.get_parameter(Name=name, WithDecryption=True)["Parameter"].get("Value", "")
    except Exception as exc:  # noqa: BLE001 - botocore's ParameterNotFound is a dynamic class
        if type(exc).__name__ != "ParameterNotFound":
            raise
        value = ""
    if not value or value.startswith("PLACEHOLDER"):
        log.warning("SSM parameter %s is unset — skipping", name)
        return 0
    os.environ.setdefault("RK_RESEND_API_KEY", value)
    os.environ.setdefault("RESEND_API_KEY", value)
    return 1


def export_for_libs(settings) -> list[str]:
    """Publish settings that third-party libraries read straight off the environment.

    `genblaze-google` resolves Vertex credentials from `GCP_PROJECT`/`GCP_LOCATION`
    itself; `providers.live_providers` reads the same names. Neither consults our
    `Settings`, so a value that arrived via the repo-root `.env` would be invisible to them —
    the same gap `ssm_path` closes for SSM, one layer further out.

    `setdefault`, not assignment: a real environment variable is a deliberate override
    and must win over the file.
    """
    published = []
    for name, value in (
        ("GCP_PROJECT", settings.gcp_project),
        ("GCP_LOCATION", settings.gcp_location),
        # `adapters/providers` reads these off the environment because `resolve()` is
        # called from three places that hold no settings object.
        ("RK_VIDEO_PROVIDER", settings.video_provider),
        ("RK_IMAGE_PROVIDER", settings.image_provider),
        ("RK_AUDIO_PROVIDER", settings.audio_provider),
    ):
        if value and name not in os.environ:
            os.environ[name] = value
            published.append(name)
    return published
