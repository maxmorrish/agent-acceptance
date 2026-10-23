"""Auth-only provisioning for one case's throwaway client config directories.

The operator names one credential file (or, for Claude Code, one token file).
Nothing else from a client home is read. Only allowlisted fields are written,
mode 0600, into the case's fresh CODEX_HOME or CLAUDE_CONFIG_DIR, and they are
removed when the case ends. Credential values never reach evidence, reports or
error text: errors carry fixed messages only.
"""
from base64 import urlsafe_b64decode
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat

from .model import strict_json

# (client, kind) -> (required basename or None, kept top-level fields, destination)
SOURCES = {
    ("codex", "file"): ("auth.json", {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}, "codex/auth.json"),
    ("claude", "file"): (".credentials.json", {"claudeAiOauth"}, "claude/.credentials.json"),
    ("claude", "token"): (None, None, "CLAUDE_CODE_OAUTH_TOKEN"),
}
# Upstream Codex refreshes ChatGPT tokens older than 8 days, and any access token
# inside a few minutes of expiry. A refresh inside a throwaway copy can rotate the
# refresh token and strand the source login, so near-refresh credentials are refused.
MAX_REFRESH_AGE = timedelta(days=7)
MIN_REMAINING = timedelta(minutes=30)


class AuthError(ValueError):
    """Fixed, value-free messages only."""


def _now(now):
    return now or datetime.now(timezone.utc)


def _jwt_expiry(token):
    try:
        part = token.split(".")[1]
        claims = json.loads(urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return datetime.fromtimestamp(int(claims["exp"]), timezone.utc)
    except (IndexError, KeyError, TypeError, ValueError):
        raise AuthError("access token expiry is unreadable") from None


def _check_fresh(client, data, now):
    now = _now(now)
    if client == "codex":
        tokens = data.get("tokens")
        if tokens is None:
            if not isinstance(data.get("OPENAI_API_KEY"), str) or not data["OPENAI_API_KEY"]:
                raise AuthError("credential file has neither tokens nor an API key")
            return
        if not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str):
            raise AuthError("credential tokens are malformed")
        try:
            refreshed = datetime.fromisoformat(str(data.get("last_refresh")).replace("Z", "+00:00"))
        except ValueError:
            raise AuthError("credential refresh time is unreadable") from None
        if refreshed.tzinfo is None:
            raise AuthError("credential refresh time is unreadable")
        if now - refreshed > MAX_REFRESH_AGE:
            raise AuthError("credential is due for refresh; refresh it in the client first")
        if _jwt_expiry(tokens["access_token"]) - now < MIN_REMAINING:
            raise AuthError("access token expires too soon; refresh it in the client first")
    else:
        oauth = data.get("claudeAiOauth")
        if not isinstance(oauth, dict) or type(oauth.get("expiresAt")) is not int:
            raise AuthError("credential file has no usable OAuth entry")
        expires = datetime.fromtimestamp(oauth["expiresAt"] / 1000, timezone.utc)
        if expires - now < MIN_REMAINING:
            raise AuthError("access token expires too soon; refresh it in the client first")


def _read_source(path):
    path = Path(path)
    if not path.is_absolute():
        raise AuthError("credential path must be absolute")
    try:
        info = path.lstat()
    except OSError:
        raise AuthError("credential file is unavailable") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise AuthError("credential must be a regular file, not a link")
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AuthError("credential file must be owned by this user with mode 0600")
    if info.st_size > 65536:
        raise AuthError("credential file is too large")
    return path.read_bytes()


@dataclass(frozen=True)
class AuthProvision:
    client: str
    kind: str
    payload: bytes = field(repr=False)

    @classmethod
    def load(cls, client, path, *, kind="file", now=None):
        if (client, kind) not in SOURCES:
            raise AuthError("no credential source of that kind for this client")
        basename, keep, _ = SOURCES[client, kind]
        if basename is not None and Path(path).name != basename:
            # Refuses config, settings, hooks, skills or memory files by name.
            raise AuthError("credential file has an unexpected name")
        raw = _read_source(path)
        if kind == "token":
            token = raw.decode("utf-8", "replace").strip()
            if not re.fullmatch(r"[A-Za-z0-9._~+/=-]{20,1024}", token):
                raise AuthError("token file must hold one token")
            return cls(client, kind, token.encode())
        try:
            data = strict_json(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise AuthError("credential file is not strict JSON") from None
        if not isinstance(data, dict):
            raise AuthError("credential file is not a JSON object")
        kept = {key: data[key] for key in sorted(keep & data.keys())}
        _check_fresh(client, kept, now)
        return cls(client, kind, json.dumps(kept).encode())

    def apply(self, workspace, *, now=None):
        """Write into this case only. Returns a value-free receipt."""
        _, _, dest = SOURCES[self.client, self.kind]
        if self.kind == "token":
            workspace.env[dest] = self.payload.decode()
        else:
            _check_fresh(self.client, json.loads(self.payload), now)
            target = workspace.root / dest
            # O_EXCL/O_NOFOLLOW: never overwrite or write through anything already there.
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(self.payload)
        return {"provisioned": True, "kind": self.kind, "removed": False, "rotated": False}

    def remove(self, workspace, receipt):
        """Remove the credential and record whether the client rewrote it."""
        _, _, dest = SOURCES[self.client, self.kind]
        if self.kind == "token":
            workspace.env.pop(dest, None)
            receipt["removed"] = dest not in workspace.env
            return receipt
        target = workspace.root / dest
        try:
            current = target.read_bytes() if not target.is_symlink() else None
        except OSError:
            current = None
        # A rewrite means the client refreshed inside the case; the source login
        # may now hold a rotated-out refresh token and need a fresh sign-in.
        receipt["rotated"] = current != self.payload
        target.unlink(missing_ok=True)
        receipt["removed"] = not target.exists() and not target.is_symlink()
        return receipt
