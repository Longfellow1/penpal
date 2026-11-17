"""Local-token auth (D15) + AccessGrant scope (§3.5.2): one random token per Host, only its sha256
is stored. A grant carries the buckets / sources / operations the Host may use, a purpose and an
optional expiry. Scope parsing fails closed: a malformed scope column grants nothing.

Serving a call needs ALL of: a token matching an active, unexpired grant; a presented client id
(env PENPAL_CLIENT) equal to that grant's client_id; that client on the user-approved whitelist
(PRD 3.8.3). Nothing is cached: every call re-reads the DB, so revoke / rotate bite immediately.

Known limitation (PRD): over stdio Penpal cannot prove who the peer process is. PENPAL_CLIENT
is a self-declared label set in the Host's config, not an attested identity; the token is the
secret, and the client check stops a token being reused under another Host's name or a Host the
user never approved. A process that has read the Host config can present both values."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from penpal.store.buckets import normalize_bucket
from penpal.store.db import get_conn

TOKEN_ENV = "PENPAL_TOKEN"
CLIENT_ENV = "PENPAL_CLIENT"
UNKNOWN_CLIENT = "unknown"
_CLIENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TOKEN_PREFIX = "kp_"
DEFAULT_BUCKETS = ("Work",)  # least privilege: a new grant reads Work only
OPERATIONS = ("read", "write")


@dataclass(frozen=True)
class Grant:
    id: int
    client_id: str
    buckets: tuple[str, ...] = ()
    sources: Optional[tuple[str, ...]] = None  # None = any source inside the granted buckets
    operations: tuple[str, ...] = ()
    purpose: Optional[str] = None
    expires_at: Optional[str] = None

    @property
    def scope_summary(self) -> str:
        return ",".join(self.buckets)

    def allows(self, operation: str) -> bool:
        return operation in self.operations


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean_list(values: Optional[Sequence[str]]) -> Optional[list[str]]:
    if values is None:
        return None
    out: list[str] = []
    for v in values:
        v = (v or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def valid_client_id(client_id: Optional[str]) -> bool:
    """Client ids are short plain labels. Token-shaped values are refused so a credential pasted
    into the wrong field can never be stored as a client name or end up in the audit log."""
    return bool(client_id) and bool(_CLIENT_RE.match(client_id)) and not client_id.startswith(_TOKEN_PREFIX)


def create_grant(
    client_id: str,
    *,
    buckets: Optional[Sequence[str]] = None,
    sources: Optional[Sequence[str]] = None,
    operations: Optional[Sequence[str]] = None,
    purpose: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> str:
    """Create a grant and return the plaintext token. The token is shown once and never stored."""
    client_id = (client_id or "").strip()
    if not client_id:
        raise ValueError("client_id must not be empty")
    if not valid_client_id(client_id):
        raise ValueError("client_id must be 1-64 chars of letters, digits, '.', '_' or '-'")
    bucket_list = [normalize_bucket(b) for b in (_clean_list(buckets) or DEFAULT_BUCKETS)]
    bucket_list = list(dict.fromkeys(bucket_list))
    source_list = _clean_list(sources)
    if source_list is not None and not source_list:
        raise ValueError("sources must not be empty (omit it to allow every source in the granted buckets)")
    op_list = _clean_list(operations) or ["read"]
    bad_ops = [o for o in op_list if o not in OPERATIONS]
    if bad_ops:
        raise ValueError(f"unknown operation {bad_ops[0]!r}; choose from {', '.join(OPERATIONS)}")
    expiry = None
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expiry = expires_at.astimezone(timezone.utc).isoformat()
        if expires_at <= _now():
            raise ValueError("expiry must be in the future")
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    conn = get_conn()
    conn.execute(
        "INSERT INTO access_grants(client_id, token_hash, created_at, buckets, sources, operations, purpose, expires_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (
            client_id,
            hash_token(token),
            _now().isoformat(),
            json.dumps(bucket_list),
            json.dumps(source_list) if source_list is not None else None,
            json.dumps(op_list),
            (purpose or "").strip() or None,
            expiry,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO client_whitelist(client_id, approved_at) VALUES (?,?)",
        (client_id, _now().isoformat()),
    )
    conn.commit()
    return token


def revoke_grants(client_id: str) -> int:
    """Revoke every active grant of the client and drop it from the whitelist."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE access_grants SET revoked_at = ? WHERE client_id = ? AND revoked_at IS NULL",
        (_now().isoformat(), client_id),
    )
    conn.execute("DELETE FROM client_whitelist WHERE client_id = ?", (client_id,))
    conn.commit()
    return cur.rowcount


def revoke_token(token: str) -> int:
    """Revoke only the grant matching this plaintext token, leaving other grants intact."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE access_grants SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
        (_now().isoformat(), hash_token(token)),
    )
    conn.commit()
    return cur.rowcount


def rotate_grant(client_id: str) -> str:
    """Issue a new token for the client's active grant (same row, same scope); the old token stops
    working at once because only one hash is ever kept per grant. Returns the new plaintext token."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, expires_at FROM access_grants WHERE client_id = ? AND revoked_at IS NULL",
        (client_id,),
    ).fetchall()
    live = [r for r in rows if not _expired(r["expires_at"])]
    if not live:
        raise ValueError(f"no active grant for client '{client_id}' (grant it first)")
    if len(live) > 1:
        raise ValueError(f"client '{client_id}' has {len(live)} active grants; revoke and re-grant to get exactly one")
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    conn.execute(
        "UPDATE access_grants SET token_hash = ?, rotated_at = ? WHERE id = ?",
        (hash_token(token), _now().isoformat(), live[0]["id"]),
    )
    conn.commit()
    return token


def is_whitelisted(client_id: str) -> bool:
    return get_conn().execute(
        "SELECT 1 FROM client_whitelist WHERE client_id = ?", (client_id,)
    ).fetchone() is not None


def list_grants() -> list[dict]:
    rows = get_conn().execute(
        "SELECT id, client_id, created_at, revoked_at, rotated_at, buckets, sources, operations, purpose, expires_at"
        " FROM access_grants ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def active_grants() -> list[dict]:
    """Grants that can still serve calls (not revoked, not expired), with the scope parsed into tuples.
    A malformed scope column reads as an empty scope, like the serving path (fails closed)."""
    out: list[dict] = []
    for row in list_grants():
        if row["revoked_at"] or _expired(row["expires_at"]):
            continue
        try:
            buckets = tuple(normalize_bucket(b) for b in (_json_tuple(row["buckets"]) or ()))
            sources, operations = _json_tuple(row["sources"]), tuple(_json_tuple(row["operations"]) or ())
        except (ValueError, TypeError):
            buckets, sources, operations = (), (), ()
        out.append({**row, "buckets": buckets, "sources": sources, "operations": operations})
    return out


def _json_tuple(raw: Optional[str]) -> Optional[tuple[str, ...]]:
    """Parse a JSON string array; raises ValueError on anything else."""
    if raw is None:
        return None
    data = json.loads(raw)
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError("not a string array")
    return tuple(data)


def _expired(expires_at: Optional[str]) -> bool:
    if not expires_at:
        return False
    try:
        when = datetime.fromisoformat(expires_at)
    except ValueError:
        return True  # unreadable expiry: fail closed
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= _now()


def _resolve(token: Optional[str]) -> tuple[Optional[Grant], str]:
    """Token-only lookup -> (grant, "ok") or (None-or-grant, reason). Reasons are for the audit
    log only; callers never see them. A rotated-away token is simply unknown (its hash is gone)."""
    if not token:
        return None, "token_missing"
    digest = hash_token(token)
    row = get_conn().execute(
        "SELECT id, client_id, token_hash, revoked_at, buckets, sources, operations, purpose, expires_at"
        " FROM access_grants WHERE token_hash = ?",
        (digest,),
    ).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], digest):
        return None, "token_unknown"
    if row["revoked_at"] is not None:
        return None, "token_revoked"
    if _expired(row["expires_at"]):
        return None, "token_expired"
    try:
        buckets = _json_tuple(row["buckets"]) or ()
        sources = _json_tuple(row["sources"])
        operations = _json_tuple(row["operations"]) or ()
        buckets = tuple(normalize_bucket(b) for b in buckets)
    except (ValueError, TypeError):
        return None, "scope_invalid"
    return Grant(
        id=row["id"],
        client_id=row["client_id"],
        buckets=buckets,
        sources=sources,
        operations=operations,
        purpose=row["purpose"],
        expires_at=row["expires_at"],
    ), "ok"


def authenticate(token: Optional[str]) -> Optional[Grant]:
    """Token-only check (no client / whitelist). Serving code must use `check_access`."""
    grant, reason = _resolve(token)
    return grant if reason == "ok" else None


@dataclass(frozen=True)
class AccessDecision:
    grant: Optional[Grant]  # set only when the call is allowed
    reason: str             # audit reason_code; never shown to the caller
    client: str             # sanitized presented client id, or "unknown"
    scope: Optional[str]    # bucket names of the matched grant, when one was found


def check_access(token: Optional[str], client: Optional[str], operation: str = "read") -> AccessDecision:
    presented = client.strip() if isinstance(client, str) else ""
    label = presented if valid_client_id(presented) else UNKNOWN_CLIENT
    grant, reason = _resolve(token)
    scope = grant.scope_summary if grant is not None else None
    if grant is None:
        return AccessDecision(None, reason, label, scope)
    if not presented:
        return AccessDecision(None, "client_missing", label, scope)
    if label == UNKNOWN_CLIENT:
        return AccessDecision(None, "client_invalid", label, scope)
    if presented != grant.client_id:
        return AccessDecision(None, "client_mismatch", label, scope)
    if not is_whitelisted(presented):
        return AccessDecision(None, "client_not_whitelisted", label, scope)
    if not grant.allows(operation):
        return AccessDecision(None, "operation_denied", label, scope)
    return AccessDecision(grant, "ok", label, scope)
