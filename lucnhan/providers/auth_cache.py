"""Cached Anthropic authentication resolution.

Avoids repeated subprocess spawns for OAuth token discovery by caching
the first successful result at module level.  This is the single authority
for auth resolution — both UI and provider construction go through here.

The anthropic_provider module (and its OAuth keychain subprocess machinery)
is only imported when auth is actually resolved, not when auth_cache is
first imported.  This keeps auth_cache import cheap.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

_cached_oauth: tuple[str, str] | None = None  # (token, auth_type)
_keychain_consent: bool = False  # set by UI after user accepts OAuth risk
_consent_generation: int = 0  # bumped on every consent change or cache invalidation
_lock = threading.Lock()  # guards the four globals above


def _get_resolver() -> Callable[..., tuple[str, str]]:
    """Lazily import and return resolve_anthropic_auth."""
    from .anthropic_provider import resolve_anthropic_auth

    return resolve_anthropic_auth


def set_keychain_consent(accepted: bool) -> None:
    """Grant or revoke consent for keychain OAuth autoload.

    Increments the consent generation so that any in-flight resolution
    that captured stale consent will discard its result.
    """
    global _keychain_consent, _cached_oauth, _consent_generation
    with _lock:
        if _keychain_consent != accepted:
            _consent_generation += 1
        _keychain_consent = accepted
        if not accepted:
            _cached_oauth = None


def resolve_auth_cached(explicit_key: str = "") -> tuple[str, str]:
    """Resolve Anthropic auth, caching the default-key result.

    * An explicit API key always bypasses the cache and is resolved fresh.
    * Cached OAuth tokens are only returned when consent is still true.
    * Returned results are validated against the consent generation that
      was current when the resolution started — if consent or generation
      changed between snapshot and return, the result is discarded and
      resolution is retried without keychain access.
    * The lock is never held during the expensive resolver / subprocess
      call (subprocess.spawn → keychain scan).
    """
    global _cached_oauth
    if explicit_key:
        return _get_resolver()(explicit_key)

    # Snapshot cache and consent info under lock.
    with _lock:
        cached = _cached_oauth
        consent = _keychain_consent
        gen = _consent_generation

    # Return a valid cached OAuth token only if consent is still granted.
    if cached is not None:
        _token, _auth_type = cached
        if _auth_type == "oauth" and not consent:
            # Cached OAuth is stale — consent was revoked after caching.
            with _lock:
                if _cached_oauth is cached:
                    _cached_oauth = None
            # Fall through to resolve fresh (without keychain).
        else:
            return cached

    # Resolve without holding the lock.
    result = _get_resolver()("", allow_keychain=consent)

    # Re-check generation / consent before caching.
    token, auth_type = result
    if token:
        with _lock:
            if _consent_generation != gen:
                # Consent or cache was invalidated during resolution —
                # the returned token may be invalid.  Do NOT cache it and
                # if the result is OAuth but consent is now false, do not
                # return it either.
                if auth_type == "oauth" and not _keychain_consent:
                    return ("", "none")
                return result
            if auth_type == "oauth" and not _keychain_consent:
                # Consent was revoked, don't cache or return OAuth.
                return ("", "none")
            _cached_oauth = result

    return result


def has_keychain_token() -> bool:
    """Check if a keychain OAuth token exists (ignoring consent)."""
    token, auth_type = _get_resolver()("", allow_keychain=True)
    return auth_type == "oauth" and bool(token)


def invalidate_cache() -> None:
    """Clear the cached auth and bump generation so the next call re-resolves."""
    global _cached_oauth, _consent_generation
    with _lock:
        _cached_oauth = None
        _consent_generation += 1
