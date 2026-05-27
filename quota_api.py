"""Claude usage/quota data layer.

Reads the OAuth token from ~/.claude/.credentials.json (kept fresh by the
claude CLI), queries the usage endpoint, and refreshes the token itself as a
safety net if it has actually expired.

No third-party dependencies — stdlib only.
"""

from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

CREDENTIALS_PATH = os.path.expanduser("~/.claude/.credentials.json")
# On macOS, Claude Code stores its OAuth token in the login Keychain rather than
# in a credentials file. When the file is absent we read/write the Keychain item
# the CLI uses, so this runs on a Mac with no extra setup.
IS_DARWIN = sys.platform == "darwin"
KEYCHAIN_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Public Claude Code OAuth client id (present in the CLI bundle).
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA = "oauth-2025-04-20"
USER_AGENT = "claude-quota-display/1.0"


class AuthError(Exception):
    """Raised when we have no usable token and cannot refresh one."""


def _read_keychain() -> dict:
    """Read the credentials JSON from the macOS login Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AuthError("no credentials in macOS Keychain (run `claude` to log in)")
    return json.loads(result.stdout)


def _write_keychain(creds: dict) -> None:
    """Update the Keychain item in place (-U), matching the CLI's account/service."""
    result = subprocess.run(
        [
            "security", "add-generic-password", "-U",
            "-s", KEYCHAIN_SERVICE,
            "-a", getpass.getuser(),
            "-w", json.dumps(creds),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AuthError(f"could not write Keychain: {result.stderr.strip()}")


def _use_keychain() -> bool:
    """The Keychain is the source on macOS when no credentials file is present.
    A file, if it exists, always wins (Linux, or an explicit export on a Mac)."""
    return IS_DARWIN and not os.path.exists(CREDENTIALS_PATH)


def _read_credentials() -> dict:
    if _use_keychain():
        return _read_keychain()
    with open(CREDENTIALS_PATH) as fh:
        return json.load(fh)


def _write_credentials(creds: dict) -> None:
    """Persist credentials back to wherever we read them from."""
    if _use_keychain():
        _write_keychain(creds)
        return
    # Atomically replace the credentials file, preserving permissions.
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


def _refresh_token() -> dict:
    """Refresh the access token using the refresh token; persist the result.

    Reads the credentials fresh (so we always use the newest refresh token the
    claude CLI may have written) and, on success, merges only the token fields
    back into a freshly re-read file — never the stale snapshot — so we cannot
    clobber other fields the CLI updated concurrently.

    Returns the updated claudeAiOauth dict. Raises AuthError on failure.
    """
    oauth = _read_credentials().get("claudeAiOauth", {})
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
        access_token = data["access_token"]
    except urllib.error.HTTPError as exc:
        raise AuthError(f"refresh failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AuthError(f"refresh failed: {exc}") from exc
    except (KeyError, ValueError) as exc:
        raise AuthError(f"refresh failed: bad response ({exc})") from exc

    # Re-read right before writing and touch only the token fields, so a
    # concurrent CLI refresh in the meantime is not reverted.
    creds = _read_credentials()
    latest = creds.setdefault("claudeAiOauth", {})
    latest["accessToken"] = access_token
    if data.get("refresh_token"):
        latest["refreshToken"] = data["refresh_token"]
    if data.get("expires_in") is not None:
        latest["expiresAt"] = int(time.time() * 1000) + int(data["expires_in"]) * 1000
    _write_credentials(creds)
    return latest


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


def _refresh_or_reload() -> dict:
    """Refresh the token, or — if our refresh loses a race with the CLI — fall
    back to whatever token the CLI just wrote to disk."""
    try:
        return _refresh_token()
    except AuthError:
        return _read_credentials().get("claudeAiOauth", {})


def fetch_usage() -> dict:
    """Fetch current usage. Refreshes the token if needed. Raises on failure."""
    oauth = _read_credentials().get("claudeAiOauth", {})

    if _is_expired(oauth):
        oauth = _refresh_or_reload()

    try:
        return _request_usage(oauth["accessToken"])
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            # Token rejected — refresh (or reload the CLI's token) and retry once.
            oauth = _refresh_or_reload()
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
