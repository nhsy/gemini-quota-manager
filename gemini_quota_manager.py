#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "google-auth>=2.29.0",
#   "requests>=2.31.0",
#   "rich>=13.7.0",
# ]
# ///
"""
gemini_quota_manager.py

List and update Google Cloud quotas for the Gemini API
(generativelanguage.googleapis.com) using the Cloud Quotas API.

Usage:
    ./gemini_quota_manager.py --project my-proj list
    ./gemini_quota_manager.py --project my-proj update --ack-decrease-risks --apply

    uv run gemini_quota_manager.py --project my-proj list

Run with -h for full options.

Exit codes:
    0  success (all updates applied or dry-run validated)
    1  argument/auth/API fatal error
    2  partial failure (one or more QuotaPreference writes failed)
   130  interrupted (Ctrl+C)
"""

from __future__ import annotations

import argparse
import hashlib
import random
import re
import sys
import threading
import time
from dataclasses import dataclass, field

import google.auth
import google.auth.transport.requests
import requests
from rich.console import Console
from rich.table import Table

CLOUDQUOTAS_API = "https://cloudquotas.googleapis.com/v1"
DEFAULT_SERVICE = "generativelanguage.googleapis.com"
DEFAULT_LOCATION = "global"
DEFAULT_FILTERS = [
    "Request limit per model per day for a project in the paid tier",
]
# Default model dimension filter: any model whose name contains "gemini".
DEFAULT_MODELS = ["gemini"]
# Models matching these rules are set to quota 0 on update (disabled).
DISABLE_QUOTA_VALUE = 0
DEFAULT_CAP_VALUE = 1000
DISABLED_MODEL_SUFFIXES = ("tts", "exp", "lite", "live")
DISABLED_MODEL_EXP_PREFIX = re.compile(r"exp-.+", re.IGNORECASE)
DISABLED_MODELS = frozenset(
    {
        "gemini-2.5-flash-native-audio-dialog",
        "gemini-2.5-flash-preview-image",
        "gemini-2.5-pro-1p-freebie",
        "gemini-3.1-flash-image",
        "gemini-3-pro-image",
    }
)
GEMINI_VERSION_PATTERN = re.compile(r"gemini-(\d+)(?:\.(\d+))?", re.IGNORECASE)
# Minimum enabled model: gemini-2.5-flash (same major.minor, tier >= flash).
MINIMUM_GEMINI_VERSION = (2, 5)
TIER_FLASH_LITE = 0
TIER_UNKNOWN = 1
TIER_FLASH = 2
TIER_PRO = 3
MINIMUM_GEMINI_TIER = TIER_FLASH
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

EXIT_SUCCESS = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 2
EXIT_INTERRUPTED = 130

console = Console()


# --------------------------------------------------------------------------
# Rate limiting
#
# The Cloud Quotas API (like most GCP APIs) enforces its own per-minute
# request limits and will return 429s if you burst too hard -- e.g. paging
# through quotaInfos, or firing off one QuotaPreference update per matched
# model. RateLimiter is a simple thread-safe token bucket that blocks
# callers until a slot is free; request_with_backoff wraps it with
# retry + exponential backoff (honoring Retry-After) for transient 429/5xx.
# --------------------------------------------------------------------------
class RateLimiter:
    """Token-bucket limiter: at most `rate` calls per `per` seconds."""

    def __init__(self, rate: float, per: float = 1.0, burst: int | None = None):
        self.rate = rate
        self.per = per
        self.capacity = burst if burst is not None else max(1, int(rate))
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._last = now
                self._tokens = min(self.capacity, self._tokens + elapsed * (self.rate / self.per))
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) * (self.per / self.rate)
            time.sleep(max(wait, 0.01))


def request_with_backoff(
    limiter: RateLimiter,
    method: str,
    url: str,
    max_retries: int = 5,
    **kwargs,
) -> requests.Response:
    """Rate-limited HTTP call with exponential backoff + jitter on 429/5xx."""
    resp = None
    for attempt in range(max_retries + 1):
        limiter.acquire()
        resp = requests.request(method, url, timeout=30, **kwargs)
        if resp.status_code not in (429, 500, 502, 503, 504):
            return resp
        if attempt == max_retries:
            return resp
        retry_after = resp.headers.get("Retry-After")
        delay = float(retry_after) if retry_after else min(30.0, 2**attempt) + random.uniform(0, 0.5)
        console.print(f"[yellow]HTTP {resp.status_code} on {method} {url.split('?')[0]} -- retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})[/yellow]")
        time.sleep(delay)
    return resp


# --------------------------------------------------------------------------
# Allowlist of common Gemini models.
# Extend/edit freely -- this gates which per-model quota dimensions the
# script is willing to look at or modify. Matching is substring-based;
# see docs/SPEC.md §7 "Model allowlist matching".
# --------------------------------------------------------------------------
# Optional curated allowlist (override defaults with --models gemini-2.5-flash ...).
ALLOWED_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3",
]


@dataclass
class QuotaMatch:
    name: str  # full resource name of the QuotaInfo
    quota_id: str
    display_name: str
    metric: str
    dimensions: dict = field(default_factory=dict)
    # Reported scalar from quotaInfos[].dimensionsInfo[].details.value
    reported_value: str | None = None
    container_type: str = "PROJECT"


def get_auth_headers(project: str) -> dict[str, str]:
    """Return ADC auth headers for Cloud Quotas API calls.

    User ADC credentials require a quota project (x-goog-user-project).
    We derive it from --project so callers don't need a separate gcloud step.
    """
    creds, _ = google.auth.default(scopes=SCOPES)
    if hasattr(creds, "with_quota_project"):
        creds = creds.with_quota_project(project)
    creds.refresh(google.auth.transport.requests.Request())
    quota_project = getattr(creds, "quota_project_id", None) or project
    return {
        "Authorization": f"Bearer {creds.token}",
        "x-goog-user-project": quota_project,
    }


def list_quota_infos(
    project: str,
    service: str,
    auth_headers: dict[str, str],
    limiter: RateLimiter,
    max_retries: int,
) -> list[dict]:
    """Page through all QuotaInfo entries for a service (rate-limited)."""
    url = f"{CLOUDQUOTAS_API}/projects/{project}/locations/{DEFAULT_LOCATION}/services/{service}/quotaInfos"
    infos: list[dict] = []
    page_token = None
    while True:
        params = {"pageSize": 200}  # API maximum page size
        if page_token:
            params["pageToken"] = page_token
        resp = request_with_backoff(limiter, "GET", url, max_retries=max_retries, headers=auth_headers, params=params)
        if resp.status_code != 200:
            console.print(f"[red]Error listing quota infos: {resp.status_code} {resp.text}[/red]")
            sys.exit(EXIT_ERROR)
        data = resp.json()
        infos.extend(data.get("quotaInfos", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return infos


def list_quota_preferences(
    project: str,
    auth_headers: dict[str, str],
    limiter: RateLimiter,
    max_retries: int,
) -> list[dict]:
    """Page through QuotaPreference resources for a project (rate-limited)."""
    url = f"{CLOUDQUOTAS_API}/projects/{project}/locations/{DEFAULT_LOCATION}/quotaPreferences"
    preferences: list[dict] = []
    page_token = None
    while True:
        params: dict[str, str | int] = {"pageSize": 200}
        if page_token:
            params["pageToken"] = page_token
        resp = request_with_backoff(limiter, "GET", url, max_retries=max_retries, headers=auth_headers, params=params)
        if resp.status_code != 200:
            console.print(f"[red]Error listing quota preferences: {resp.status_code} {resp.text}[/red]")
            sys.exit(EXIT_ERROR)
        data = resp.json()
        preferences.extend(data.get("quotaPreferences", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return preferences


def preference_lookup_key(service: str, quota_id: str, dimensions: dict) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    return (service, quota_id, tuple(sorted(dimensions.items())))


def build_preference_lookup(preferences: list[dict]) -> dict[tuple[str, str, tuple[tuple[str, str], ...]], str]:
    """Map (service, quotaId, dimensions) -> full QuotaPreference resource name."""
    lookup: dict[tuple[str, str, tuple[tuple[str, str], ...]], str] = {}
    for pref in preferences:
        name = pref.get("name")
        if not name:
            continue
        dims = pref.get("dimensions") or {}
        key = preference_lookup_key(pref.get("service", ""), pref.get("quotaId", ""), dims)
        lookup[key] = name
    return lookup


def resolve_quota_preference(
    project: str,
    service: str,
    match: QuotaMatch,
    preference_lookup: dict[tuple[str, str, tuple[tuple[str, str], ...]], str] | None,
) -> tuple[str, str]:
    """Return (full resource name, preference id) for PATCH."""
    if preference_lookup:
        key = preference_lookup_key(service, match.quota_id, match.dimensions)
        existing_name = preference_lookup.get(key)
        if existing_name:
            return existing_name, existing_name.rsplit("/", 1)[-1]
    pref_id = build_quota_preference_id(match)
    return quota_preference_name(project, pref_id), pref_id


def dimensions_infos_from_quota(info: dict) -> list[dict]:
    """Per-model dimension rows from QuotaInfo (API field is dimensionsInfos)."""
    return info.get("dimensionsInfos") or info.get("dimensionsInfo") or []


def matches_filters(display_name: str, filters: list[str]) -> bool:
    lowered = display_name.lower()
    return all(f.lower() in lowered for f in filters)


def model_from_dimensions(dims: dict) -> str | None:
    # The "model" dimension key is how Gemini/Vertex quotas key per-model limits.
    for key in ("model", "base_model", "model_id"):
        if key in dims:
            return dims[key]
    return None


def model_matches_allowlist(model: str, allowed_models: list[str]) -> bool:
    """Substring match: any allowlist entry contained in the model dimension."""
    return any(m in model for m in allowed_models)


def gemini_model_tier(model: str) -> tuple[int, int, int] | None:
    """Return ``(major, minor, tier_rank)`` for versioned Gemini model names."""
    lowered = model.lower()
    match = GEMINI_VERSION_PATTERN.search(lowered)
    if not match:
        return None

    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) is not None else 0
    suffix = lowered[match.end() :]

    if re.search(r"-pro(?:-|$)", suffix):
        tier = TIER_PRO
    elif "flash-lite" in suffix or re.search(r"-lite(?:-|$)", suffix) or "flash-8b" in suffix or re.search(r"-8b(?:-|$)", suffix):
        tier = TIER_FLASH_LITE
    elif "flash" in suffix:
        tier = TIER_FLASH
    else:
        tier = TIER_UNKNOWN
    return major, minor, tier


def model_is_below_minimum_version(model: str) -> bool:
    """True when a Gemini model is older than gemini-2.5-flash."""
    tier_info = gemini_model_tier(model)
    if tier_info is None:
        return "gemini" in model.lower()

    major, minor, tier = tier_info
    version = (major, minor)
    if version < MINIMUM_GEMINI_VERSION:
        return True
    if version > MINIMUM_GEMINI_VERSION:
        return tier < MINIMUM_GEMINI_TIER
    return tier < MINIMUM_GEMINI_TIER


def model_disable_reason(model: str) -> str | None:
    lowered = model.lower()
    if lowered in DISABLED_MODELS:
        return "explicitly disabled"
    if lowered.endswith(DISABLED_MODEL_SUFFIXES):
        return f"disabled suffix ({', '.join(DISABLED_MODEL_SUFFIXES)})"
    if DISABLED_MODEL_EXP_PREFIX.search(lowered):
        return "exp-*"
    if model_is_below_minimum_version(model):
        return f"below gemini-{MINIMUM_GEMINI_VERSION[0]}.{MINIMUM_GEMINI_VERSION[1]}-flash"
    return None


def model_should_disable(model: str) -> bool:
    """True when a model dimension should be disabled (quota set to 0).

    Rules (case-insensitive):
      - explicit denylist (see ``DISABLED_MODELS``)
      - suffix ``tts``  (e.g. gemini-2.5-flash-tts)
      - suffix ``exp``  (e.g. computer-use-exp)
      - suffix ``lite`` (e.g. gemini-2.5-flash-lite)
      - suffix ``live`` (e.g. gemini-2.5-flash-live)
      - ``exp-*``       (``exp-`` followed by more characters anywhere in the name)
      - below ``gemini-2.5-flash`` (older versions or lower tiers such as flash-lite)
    """
    return model_disable_reason(model) is not None


def target_value_for_model(model: str | None, default_value: int) -> int:
    if model is not None and model_should_disable(model):
        return DISABLE_QUOTA_VALUE
    return default_value


UNLIMITED_MARKERS = {"-1", "unlimited", "none", "inf", "infinite"}


def is_unlimited(match: QuotaMatch) -> bool:
    """True if the QuotaInfo reports this quota as unlimited.

    The Cloud Quotas API represents "no limit" as a value of -1; the Cloud
    Console sometimes renders that as the word "Unlimited" instead, so both
    forms are checked.
    """
    if match.reported_value is None:
        return False
    return match.reported_value.strip().lower() in UNLIMITED_MARKERS


def current_numeric_value(match: QuotaMatch) -> float | None:
    """Best-effort parse of reported_value to a number.

    Returns None if the value is missing, unlimited, or otherwise
    non-numeric. Use is_unlimited() separately — unlimited quotas should
    still be reduced to the target.
    """
    if match.reported_value is None or is_unlimited(match):
        return None
    try:
        return float(match.reported_value)
    except (TypeError, ValueError):
        return None


def find_matching_quotas(
    project: str,
    service: str,
    auth_headers: dict[str, str],
    filters: list[str],
    allowed_models: list[str],
    exclude_models: list[str],
    limiter: RateLimiter,
    max_retries: int,
) -> list[QuotaMatch]:
    matches: list[QuotaMatch] = []
    for info in list_quota_infos(project, service, auth_headers, limiter, max_retries):
        display_name = info.get("quotaDisplayName") or info.get("metricDisplayName") or ""
        if not matches_filters(display_name, filters):
            continue

        for dim_entry in dimensions_infos_from_quota(info):
            dims = dim_entry.get("dimensions", {})
            model = model_from_dimensions(dims)

            if model is not None:
                if exclude_models and any(x in model for x in exclude_models):
                    continue
                if not model_matches_allowlist(model, allowed_models):
                    continue

            matches.append(
                QuotaMatch(
                    name=info.get("name", ""),
                    quota_id=info.get("quotaId", ""),
                    display_name=display_name,
                    metric=info.get("metric", ""),
                    dimensions=dims,
                    reported_value=str(dim_entry.get("details", {}).get("value", "n/a")),
                    container_type=info.get("containerType", "PROJECT"),
                )
            )
    return matches


def format_reported_value(match: QuotaMatch) -> str:
    """Human-readable reported quota limit for table output."""
    if match.reported_value is None:
        return "-"
    if is_unlimited(match):
        return "-1 (unlimited)"
    return match.reported_value


def format_policy(model: str | None, cap_value: int, match: QuotaMatch) -> str:
    """Update policy label for table output."""
    target = target_value_for_model(model, cap_value)
    if target == DISABLE_QUOTA_VALUE:
        return "disable → 0"
    current = current_numeric_value(match)
    if current is not None and current < target:
        displayed = int(current) if current == int(current) else current
        return f"leave → {displayed}"
    return f"cap → {target}"


def print_matches(matches: list[QuotaMatch], cap_value: int = DEFAULT_CAP_VALUE) -> None:
    table = Table(title="Matching Gemini API Quotas")
    table.add_column("Quota ID", overflow="fold")
    table.add_column("Display Name", overflow="fold")
    table.add_column("Model dim.")
    table.add_column("Reported value")
    table.add_column("Policy")

    for m in matches:
        model = model_from_dimensions(m.dimensions)
        table.add_row(
            m.quota_id,
            m.display_name,
            model or "-",
            format_reported_value(m),
            format_policy(model, cap_value, m),
        )
    console.print(table)
    console.print(f"[bold]{len(matches)}[/bold] quota(s) matched.")


def build_quota_preference_id(match: QuotaMatch) -> str:
    """Stable preference ID; truncated to 63 chars with hash suffix if needed."""
    dim_suffix = "-".join(f"{k}-{v}" for k, v in sorted(match.dimensions.items()))
    base = f"{match.quota_id}-{dim_suffix}".strip("-")
    if len(base) <= 63:
        return base
    digest = hashlib.sha256(base.encode()).hexdigest()[:8]
    return f"{base[:54]}-{digest}"


def build_quota_preference_body(
    resource_name: str,
    service: str,
    match: QuotaMatch,
    value: int,
) -> dict:
    """Request body for a QuotaPreference upsert."""
    return {
        "name": resource_name,
        "dimensions": match.dimensions,
        "quotaConfig": {"preferredValue": value},
        "service": service,
        "quotaId": match.quota_id,
    }


def quota_preference_name(project: str, pref_id: str) -> str:
    return f"projects/{project}/locations/{DEFAULT_LOCATION}/quotaPreferences/{pref_id}"


def apply_update(
    project: str,
    service: str,
    auth_headers: dict[str, str],
    match: QuotaMatch,
    value: int,
    dry_run: bool,
    limiter: RateLimiter,
    max_retries: int,
    ignore_safety_checks: list[str] | None = None,
    preference_lookup: dict[tuple[str, str, tuple[tuple[str, str], ...]], str] | None = None,
) -> bool:
    """Returns True on success (2xx), False on failure."""
    resource_name, _pref_id = resolve_quota_preference(project, service, match, preference_lookup)
    body = build_quota_preference_body(resource_name, service, match, value)
    url = f"{CLOUDQUOTAS_API}/{resource_name}"
    params: dict[str, str | list[str]] = {
        "validateOnly": "true" if dry_run else "false",
    }
    if preference_lookup_key(service, match.quota_id, match.dimensions) not in (preference_lookup or {}):
        params["allowMissing"] = "true"
    if ignore_safety_checks:
        params["ignoreSafetyChecks"] = ignore_safety_checks
    headers = {**auth_headers, "Content-Type": "application/json"}

    resp = request_with_backoff(
        limiter,
        "PATCH",
        url,
        max_retries=max_retries,
        headers=headers,
        params=params,
        json=body,
    )
    action = "[yellow]DRY-RUN[/yellow]" if dry_run else "[green]APPLIED[/green]"
    model_label = model_from_dimensions(match.dimensions) or "project-wide"
    if resp.status_code in (200, 201):
        console.print(f"{action} {match.quota_id} ({model_label}) -> {value}")
        return True
    console.print(f"[red]FAILED[/red] {match.quota_id}: {resp.status_code} {resp.text}")
    return False


def build_limiter(args: argparse.Namespace) -> RateLimiter:
    return RateLimiter(rate=args.rps, per=1.0, burst=args.burst)


def validate_decrease_ack(args: argparse.Namespace) -> None:
    """Require explicit decrease-risk acknowledgment before any update API call."""
    ack = getattr(args, "ack_decrease_risks", False)
    high = getattr(args, "allow_high_percentage_quota_decrease", False)
    below = getattr(args, "allow_quota_decrease_below_usage", False)
    if ack or (high and below):
        return
    console.print("[red]Decrease-risk acknowledgment required.[/red] Pass --ack-decrease-risks or both --allow-high-percentage-quota-decrease and --allow-quota-decrease-below-usage.")
    sys.exit(EXIT_ERROR)


def cmd_list(args: argparse.Namespace) -> None:
    auth_headers = get_auth_headers(args.project)
    limiter = build_limiter(args)
    matches = find_matching_quotas(
        args.project,
        args.service,
        auth_headers,
        args.filters,
        args.models,
        args.exclude_models,
        limiter,
        args.max_retries,
    )
    print_matches(matches, cap_value=args.value)


def process_quota_update(
    match: QuotaMatch,
    target_value: int,
    args: argparse.Namespace,
    auth_headers: dict[str, str],
    limiter: RateLimiter,
    dry_run: bool,
    ignore_safety_checks: list[str],
    preference_lookup: dict[tuple[str, str, tuple[tuple[str, str], ...]], str] | None = None,
) -> tuple[str, bool]:
    """Apply one quota update. Returns (outcome, success)."""
    model_label = model_from_dimensions(match.dimensions) or "project-wide"
    disabling = target_value == DISABLE_QUOTA_VALUE and model_label != "project-wide"

    if is_unlimited(match):
        label = "DISABLE" if disabling else "UNLIMITED"
        console.print(f"[magenta]{label} -> {target_value}[/magenta] {match.quota_id} ({model_label}): currently unlimited; will be capped.")
    else:
        current = current_numeric_value(match)
        if current is not None and current == target_value:
            console.print(f"[cyan]SKIP[/cyan] {match.quota_id} ({model_label}): reported value {match.reported_value} already equals target {target_value}.")
            return ("skipped_at_target", False)
        if disabling:
            reason = model_disable_reason(model_label) or "disable rule"
            console.print(f"[red]DISABLE[/red] {match.quota_id} ({model_label}): {reason} -> {target_value}.")
        elif current is None:
            if args.skip_unknown_values:
                console.print(f"[cyan]SKIP[/cyan] {match.quota_id} ({model_label}): reported value {match.reported_value!r} is not numeric; --skip-unknown-values set.")
                return ("skipped_unknown", False)
            console.print(f"[yellow]UNKNOWN[/yellow] {match.quota_id} ({model_label}): reported value {match.reported_value!r} is not numeric; proceeding.")
        elif current < target_value:
            console.print(f"[cyan]SKIP[/cyan] {match.quota_id} ({model_label}): reported value {match.reported_value} is below cap {target_value}; leaving untouched.")
            return ("skipped_below_cap", False)

    ok = apply_update(
        args.project,
        args.service,
        auth_headers,
        match,
        target_value,
        dry_run,
        limiter,
        args.max_retries,
        ignore_safety_checks,
        preference_lookup,
    )
    return ("applied", ok)


def cmd_update(args: argparse.Namespace) -> None:
    validate_decrease_ack(args)
    auth_headers = get_auth_headers(args.project)
    limiter = build_limiter(args)
    matches = find_matching_quotas(
        args.project,
        args.service,
        auth_headers,
        args.filters,
        args.models,
        args.exclude_models,
        limiter,
        args.max_retries,
    )
    if not matches:
        console.print("[yellow]No matching quotas found. Nothing to update.[/yellow]")
        return

    print_matches(matches, cap_value=args.value)
    dry_run = not args.apply
    if dry_run:
        console.print("\n[bold yellow]Dry-run mode[/bold yellow] (validateOnly=true; no changes submitted). Pass --apply to submit quota preference updates.\n")

    ignore_safety_checks = [
        "QUOTA_DECREASE_PERCENTAGE_TOO_HIGH",
        "QUOTA_DECREASE_BELOW_USAGE",
    ]

    console.print("[dim]Loading existing quota preferences…[/dim]")
    preference_lookup = build_preference_lookup(
        list_quota_preferences(args.project, auth_headers, limiter, args.max_retries),
    )
    console.print(f"[dim]Found {len(preference_lookup)} existing preference(s).[/dim]\n")

    skipped = 0
    unknown_skipped = 0
    disabled = 0
    applied = 0
    failed = 0
    interrupted = False

    try:
        for match in matches:
            model = model_from_dimensions(match.dimensions)
            target_value = target_value_for_model(model, args.value)
            outcome, ok = process_quota_update(
                match,
                target_value,
                args,
                auth_headers,
                limiter,
                dry_run,
                ignore_safety_checks,
                preference_lookup,
            )
            if outcome == "skipped_at_target":
                skipped += 1
            elif outcome == "skipped_unknown":
                unknown_skipped += 1
            elif outcome == "skipped_below_cap":
                skipped += 1
            elif outcome == "applied":
                if target_value == DISABLE_QUOTA_VALUE and model is not None:
                    disabled += 1
                if ok:
                    applied += 1
                else:
                    failed += 1
    except KeyboardInterrupt:
        interrupted = True
        console.print("\n[yellow]Interrupted (Ctrl+C). Stopping; no further quota updates will be submitted.[/yellow]")

    console.print(
        f"\n[bold]Summary:[/bold] {applied} applied/validated, {disabled} disabled (→0), {skipped} skipped (at target/below cap), {unknown_skipped} skipped (unknown value), {failed} failed."
    )
    if interrupted:
        sys.exit(EXIT_INTERRUPTED)
    if failed:
        sys.exit(EXIT_PARTIAL)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage Gemini API quotas on Google Cloud.")
    parser.add_argument("--project", required=True, help="GCP project ID")
    parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE,
        help="Service name (default: generativelanguage.googleapis.com)",
    )
    parser.add_argument(
        "--filters",
        nargs="+",
        default=DEFAULT_FILTERS,
        help=("Case-insensitive substrings that must ALL appear in a quota display name (default: paid-tier daily request limit per model)"),
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=DEFAULT_MODELS,
        help="Allowlist of model name substrings (default: gemini)",
    )
    parser.add_argument(
        "--exclude-models",
        nargs="+",
        default=[],
        help="Denylist of model name substrings; applied before the allowlist",
    )
    parser.add_argument(
        "--rps",
        type=float,
        default=4.0,
        help="Max requests per second to the Cloud Quotas API (default: 4.0)",
    )
    parser.add_argument(
        "--burst",
        type=int,
        default=None,
        help="Token-bucket burst capacity (default: ceil(--rps))",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Max retries with exponential backoff on 429/5xx responses (default: 5)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List matching quotas")
    p_list.add_argument("--value", type=int, default=DEFAULT_CAP_VALUE, help=f"Cap shown in Policy column (default: {DEFAULT_CAP_VALUE})")
    p_list.set_defaults(func=cmd_list)

    p_update = sub.add_parser("update", help="Set matching quotas to a target value")
    p_update.add_argument("--value", type=int, default=DEFAULT_CAP_VALUE, help=f"Target preferredValue (default: {DEFAULT_CAP_VALUE})")
    p_update.add_argument(
        "--skip-unknown-values",
        action="store_true",
        help=("Skip quotas whose reported value is missing or non-numeric instead of proceeding"),
    )
    p_update.add_argument(
        "--apply",
        action="store_true",
        help="Submit changes (validateOnly=false). Without this flag, dry-run only.",
    )
    p_update.add_argument(
        "--ack-decrease-risks",
        action="store_true",
        help=("Shorthand for both decrease safety checks. Same as passing --allow-high-percentage-quota-decrease and --allow-quota-decrease-below-usage."),
    )
    p_update.add_argument(
        "--allow-high-percentage-quota-decrease",
        action="store_true",
        help=("Sets ignoreSafetyChecks=QUOTA_DECREASE_PERCENTAGE_TOO_HIGH (requires --allow-quota-decrease-below-usage or --ack-decrease-risks)"),
    )
    p_update.add_argument(
        "--allow-quota-decrease-below-usage",
        action="store_true",
        help=("Sets ignoreSafetyChecks=QUOTA_DECREASE_BELOW_USAGE (requires --allow-high-percentage-quota-decrease or --ack-decrease-risks)"),
    )
    p_update.set_defaults(func=cmd_update)

    return parser.parse_args()


def main() -> None:
    try:
        args = parse_args()
        args.func(args)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted (Ctrl+C).[/yellow]")
        sys.exit(EXIT_INTERRUPTED)


if __name__ == "__main__":
    main()
