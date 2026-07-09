"""AI provider abstraction — Bedrock and direct Anthropic.

This module is intentionally dependency-free of web/FastAPI code so it can be
reused by the setup wizard and other CLI tooling.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class CredentialResolutionError(ValueError):
    """AWS credential resolution failed with a user-actionable message.

    Carries a single-line ``message`` that the wizard's Test-AI button
    can surface verbatim — e.g. ``"AWS profile 'flixbus-it': The config
    profile (flixbus-it) could not be found"`` instead of the generic
    "credentials are required" string.
    """


def resolve_aws_credentials(settings) -> Optional[Dict[str, str]]:
    """Resolve credentials for AsyncAnthropicBedrock.

    Three modes, in priority order:
      1. AWS_PROFILE set → boto3 reads it from ~/.aws/credentials, including
         SSO/Azure-AD-refreshed temporary credentials. Returns frozen
         access_key/secret_key/session_token for the SDK.
      2. Static AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY in env (plus
         optional AWS_SESSION_TOKEN for STS-temporary keys).
      3. None → caller must skip the filter.

    Raises:
        CredentialResolutionError: when profile mode is selected but boto3
        cannot resolve it (typo in profile name, expired SSO session, etc.).
        The wizard surfaces the exception's ``message`` in the Test-AI
        response so users know exactly what to fix.
    """
    if settings.aws_profile:
        try:
            import boto3  # local import: only needed when profile auth is used
            from botocore.exceptions import (  # local: same reason
                BotoCoreError,
                ProfileNotFound,
            )
        except ImportError:  # pragma: no cover - boto3 is a required dep
            return None
        try:
            session = boto3.Session(profile_name=settings.aws_profile)
        except ProfileNotFound as exc:
            raise CredentialResolutionError(
                f"AWS profile '{settings.aws_profile}' not found in "
                f"~/.aws/config. {exc}"
            ) from exc
        try:
            creds = session.get_credentials()
        except (BotoCoreError, Exception) as exc:  # pragma: no cover
            raise CredentialResolutionError(
                f"AWS profile '{settings.aws_profile}': failed to load "
                f"credentials — {exc}"
            ) from exc
        if creds is None:
            raise CredentialResolutionError(
                f"AWS profile '{settings.aws_profile}' resolved no "
                "credentials. Did you log in via aws-azure-login (or your "
                "SSO tool)?"
            )
        try:
            frozen = creds.get_frozen_credentials()
        except Exception as exc:  # pragma: no cover
            raise CredentialResolutionError(
                f"AWS profile '{settings.aws_profile}': "
                f"credentials refresh failed — {exc}"
            ) from exc
        return {
            "aws_access_key": frozen.access_key,
            "aws_secret_key": frozen.secret_key,
            "aws_session_token": frozen.token,
            "aws_region": settings.aws_region,
        }
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        result = {
            "aws_access_key": settings.aws_access_key_id,
            "aws_secret_key": settings.aws_secret_access_key,
            "aws_region": settings.aws_region,
        }
        # STS-temporary keys (ASIA-prefixed) require the session token.
        # Permanent IAM keys (AKIA) ignore it. Pass it through when set.
        if settings.aws_session_token:
            result["aws_session_token"] = settings.aws_session_token
        return result
    return None


def create_ai_client(settings):
    """Return an async Anthropic client for the configured provider.

    Returns:
        AsyncAnthropicBedrock when settings.ai_provider == "bedrock"
        AsyncAnthropic when settings.ai_provider == "anthropic"

    Raises:
        ValueError: if ai_provider is invalid or if bedrock is selected but AWS credentials cannot be resolved.
        CredentialResolutionError: profile-mode credential resolution failed.
    """
    if settings.ai_provider not in ("bedrock", "anthropic"):
        raise ValueError(
            f"Invalid AI_PROVIDER {settings.ai_provider!r} — must be 'bedrock' or 'anthropic'"
        )

    if settings.ai_provider == "anthropic":
        from anthropic import AsyncAnthropic
        return AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Default: bedrock
    from anthropic import AsyncAnthropicBedrock
    aws_kwargs = resolve_aws_credentials(settings)
    if aws_kwargs is None:
        raise ValueError(
            "AWS credentials are required for the 'bedrock' provider but none were found. "
            "Set AWS_PROFILE or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY in your .env."
        )
    return AsyncAnthropicBedrock(**aws_kwargs)


# ---------------------------------------------------------------------------
# Error-message friendlifier (Bedrock-specific)
# ---------------------------------------------------------------------------
_ARN_PATTERN = re.compile(r"arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:[^\s\"']+")
_ACCOUNT_ID_PATTERN = re.compile(r"\b\d{12}\b")


def _friendlier_bedrock_error(raw: str) -> str:
    """Turn a raw boto3/httpx exception message into an actionable hint.

    Also redacts AWS ARNs and 12-digit account IDs so the Test-AI response
    body can't be scraped for recon via DNS rebinding (belt-and-braces —
    TrustedHost also blocks that path in v0.6.1).
    """
    if not raw:
        return raw
    lowered = raw.lower()
    hint: Optional[str] = None
    if "expiredtoken" in lowered or "expired token" in lowered or "token is expired" in lowered:
        hint = (
            "Your AWS SSO session has expired. Run "
            "`aws-azure-login` (or your SSO tool), then click Test AI again."
        )
    elif "accessdenied" in lowered or "not authorized" in lowered or "403" in lowered:
        hint = (
            "Your AWS account is missing Bedrock InvokeModel permission "
            "on the configured model in eu-west-1. Ask FlixBus IT to grant "
            "`bedrock:InvokeModel` on the Haiku inference profile ARN."
        )
    elif "could not connect" in lowered or "endpoint" in lowered and "connection" in lowered:
        hint = (
            "Cannot reach Bedrock — likely a corporate proxy or offline. "
            "Check with IT that eu-west-1 Bedrock endpoints are allowlisted."
        )
    elif "unrecognizedclientexception" in lowered or "invalidsignatureexception" in lowered:
        hint = (
            "AWS rejected the credentials as invalid. Double-check the "
            "profile name or the access-key/secret pair in your .env."
        )
    # Redact ARNs and account IDs from whatever the underlying message
    # was — even without a hint. Truncate long messages to 400 chars.
    redacted = _ARN_PATTERN.sub("arn:aws:***", raw)
    redacted = _ACCOUNT_ID_PATTERN.sub("<account-id>", redacted)
    if len(redacted) > 400:
        redacted = redacted[:400] + "…"
    if hint:
        return f"{hint} (raw: {redacted})"
    return redacted


async def test_connection(settings) -> Dict[str, Any]:
    """Probe the configured AI provider with a tiny request.

    Returns a dict with:
        ok (bool): True if the call succeeded.
        model (str): Model ID echoed by the API (or the configured model on error).
        latency_ms (int): Round-trip time in milliseconds.
        error (str | None): User-friendly error message on failure.
    """
    model = settings.anthropic_model
    t0 = time.monotonic()
    error: Optional[str] = None
    try:
        client = create_ai_client(settings)
        response = await client.messages.create(
            model=model,
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        # Use model echoed by response if available
        echoed_model = getattr(response, "model", model)
        if echoed_model:
            model = echoed_model
        ok = True
    except CredentialResolutionError as exc:
        # Profile-not-found / SSO-expired: message is already user-friendly.
        ok = False
        error = str(exc)
    except Exception as exc:
        ok = False
        error = _friendlier_bedrock_error(str(exc))

    latency_ms = int((time.monotonic() - t0) * 1000)
    return {
        "ok": ok,
        "model": model,
        "latency_ms": latency_ms,
        "error": error,
    }
