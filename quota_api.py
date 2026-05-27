"""Claude usage/quota data layer.

Reads the OAuth token from ~/.claude/.credentials.json (kept fresh by the
claude CLI), queries the usage endpoint, and refreshes the token itself as a
safety net if it has actually expired.

No third-party dependencies — stdlib only.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.request
import urllib.error

CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Public Claude Code OAuth client id (present in the CLI bundle).
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "claude-quota-display/1.0"


class AuthError(Exception):
    """Raised when we have no usable token and cannot refresh one."""


def _read_credentials() -> dict:
    with open(CREDENTIALS_PATH) as fh:
        return json.load(fh)


def _write_credentials(creds: dict) -> None:
    """Atomically replace the credentials file, preserving permissions."""
    directory = os.path.dirname(CREDENTIALS_PATH)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".credentials.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(creds, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CREDENTIALS_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _refresh_token(creds: dict) -> dict:
    """Refresh the access token using the refresh token; persist the result.

    Returns the updated claudeAiOauth dict. Raises AuthError on failure.
    """
    oauth = creds["claudeAiOauth"]
    refresh_token = oauth.get("refreshToken")
    if not refresh_token:
        raise AuthError("no refresh token available")

    payload = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise AuthError(f"refresh failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AuthError(f"refresh failed: {exc}") from exc

    oauth["accessToken"] = data["access_token"]
    if data.get("refresh_token"):
        oauth["refreshToken"] = data["refresh_token"]
    if data.get("expires_in"):
        oauth["expiresAt"] = int(time.time() * 1000) + int(data["expires_in"]) * 1000
    _write_credentials(creds)
    return oauth


def _is_expired(oauth: dict, skew_ms: int = 60_000) -> bool:
    expires_at = oauth.get("expiresAt")
    if not expires_at:
        return False
    return time.time() * 1000 >= (expires_at - skew_ms)


def _request_usage(access_token: str) -> dict:
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def fetch_usage() -> dict:
    """Fetch current usage. Refreshes the token if needed. Raises on failure."""
    creds = _read_credentials()
    oauth = creds["claudeAiOauth"]

    if _is_expired(oauth):
        oauth = _refresh_token(creds)

    try:
        return _request_usage(oauth["accessToken"])
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # Token rejected — try one refresh then retry once.
            oauth = _refresh_token(creds)
            return _request_usage(oauth["accessToken"])
        raise


if __name__ == "__main__":
    import sys

    try:
        usage = fetch_usage()
        json.dump(usage, sys.stdout, indent=2)
        print()
    except Exception as exc:  # noqa: BLE001 - CLI surface
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
