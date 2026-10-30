"""Tool implementations, independent of the MCP transport (unit-testable)."""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from penpal.mcp import audit
from penpal.mcp.auth import Grant, check_access
from penpal.foundation.desensitizer import mask_emails, redact_secrets
from penpal.search.engine import (
    TIMELINE_DEFAULT_LIMIT, TIMELINE_MAX_LIMIT, keyword_in_scope, normalize_keywords, parse_time_bound, scoped_evidence,
    search_events, source_coverage, timeline_events,
)
from penpal.foundation.sqlite_rules import is_blocked_sqlite
from penpal.sources.cleaning.path_filter import is_excluded_path
from penpal.sources.registry import unavailable_sources
from penpal.store import feedback
from penpal.store.db import get_conn
from penpal.store.capture_events import CAPTURE_SEARCHABLE_EVENT_TYPES, is_penpal_locator
from penpal.store.evidence import (
    is_unresolvable_locator, refresh_availability, split_browser_locator, split_git_locator, unresolvable_source,
)
from penpal.store.evidence_original import ORIGINAL_SOURCES, read_original

# What a granted Host can search (D26): source-plugin events plus Penpal's own capture streams
# (keyboard chunks, window focus sessions, clipboard, AX text, browser URLs), all still limited by
# bucket + source scope. Capture rows are classified by app (store.buckets / store.app_categories)
# and secret-redacted at write time. idle_* carry no content and OCR / manual notes are out of scope.
SEARCHABLE_EVENT_TYPES = ("semantic_event", *CAPTURE_SEARCHABLE_EVENT_TYPES)
MAX_KEYWORDS = 10
MAX_KEYWORD_CHARS = 100
# A keyword that looks like a whole sentence means the host skipped the keyword extraction step.
MAX_KEYWORD_CJK_CHARS = 12
MAX_KEYWORD_WORDS = 6
TOOL_SEARCH_CONTEXT = "search_context"
TOOL_GET_EVIDENCE = "get_evidence"
TOOL_GET_TIMELINE = "get_timeline"
TOOL_RATE_RESULT = "rate_result"
TOOL_COMMIT_MEMORY = "commit_memory"
TOOL_UPDATE_MEMORY = "update_memory"
EXCERPT_CHARS = 400
EVIDENCE_NOT_FOUND = {"status": "not_found", "message": "No evidence with this id is available to this client."}
SOURCE_UNAVAILABLE_MESSAGE = "原始出处已不可访问 / original source is no longer accessible"
# Sources whose events have no openable original by design (stored with available=0, locator
# "unresolvable://<source>"). A fixed reason per source; never an error.
NOT_RESOLVABLE_MESSAGES = {
    "knowledgec": "macOS 系统活动库只有聚合记录，没有可打开的单条原文 / knowledgeC holds system activity records with no openable original",
    "approved_sqlite": "用户授权的第三方数据库行，不提供原文回溯 / rows from a user-approved third-party database have no openable original",
}
NOT_RESOLVABLE_DEFAULT = "该来源没有可打开的原始出处 / this source has no openable original"

_MATCH_REASON = "keyword match"
SENTENCE_KEYWORD_MESSAGE = (
    "Each keyword must be a short word or phrase, not a sentence or question. "
    "Split the question into 1-6 distinctive keywords and call again."
)
KEYWORDS_MESSAGE = "keywords must be a non-empty list of 1-10 strings"


def _looks_like_sentence(keyword: str) -> bool:
    cjk = sum(1 for ch in keyword if "\u4e00" <= ch <= "\u9fff")
    return (
        keyword.endswith(("?", "\uff1f"))
        or cjk > MAX_KEYWORD_CJK_CHARS
        or len(keyword.split()) > MAX_KEYWORD_WORDS
        or len(keyword) > MAX_KEYWORD_CHARS
    )


_TEXT_FIELDS = frozenset({"snippet", "excerpt", "title", "app", "app_name", "window_title", "url", "text"})


# privacy.redact_emails, applied at query time too (default on = fail safe). `penpal mcp serve` sets it
# once from the loaded config; there is no per-call or Host-side way to turn it off.
_mask_emails_at_query = True


def configure(*, mask_emails: bool) -> None:
    global _mask_emails_at_query
    _mask_emails_at_query = bool(mask_emails)


def _redact_text(text: str) -> str:
    text = redact_secrets(text)
    return mask_emails(text) if _mask_emails_at_query else text


def _redact_fields(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _redact_text(v) if k in _TEXT_FIELDS and isinstance(v, str) else _redact_fields(v)
                for k, v in node.items()}
    if isinstance(node, list):
        return [_redact_fields(x) for x in node]
    return node


def redaction_failed() -> dict[str, Any]:
    """Uniform body (same shape as permission_denied) when the query-time redaction pass fails."""
    return {
        "status": "error",
        "reason_code": "REDACTION_UNAVAILABLE",
        "message": "Penpal could not safely prepare this response.",
        "next_action": "retry_later",
    }


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Second, query-time redaction pass over every text field returned to the Host (backstop for rows
    stored before current rules existed). Fails closed: any error returns a generic body, never text."""
    try:
        return _redact_fields(payload)
    except Exception:
        return redaction_failed()


def permission_denied() -> dict[str, Any]:
    """One body for EVERY deny reason (missing / wrong / revoked / rotated / expired token, bad or
    non-whitelisted client, no read right): no data, counts or hints. Reasons live in the audit log only."""
    return {
        "status": "permission_denied",
        "reason_code": "TOKEN_INVALID",
        "message": "Penpal access was not granted for this client.",
        "next_action": "review_access_in_penpal",
    }


def _parse_time_range(time_range: Any) -> tuple[Optional[str], Optional[str]]:
    if time_range is None:
        return None, None
    if not isinstance(time_range, dict):
        raise ValueError("time_range must be an object with optional 'start' and 'end'")
    unknown = set(time_range) - {"start", "end"}
    if unknown:
        raise ValueError("time_range accepts only 'start' and 'end'")
    return parse_time_bound(time_range.get("start")), parse_time_bound(time_range.get("end"), end_of_day=True)


def _coverage(grant: Grant, start: Optional[str], end: Optional[str]) -> dict[str, Any]:
    """What this call searched: the caller's own grant scope only. Must never carry names, counts
    or existence of anything outside it (B1 / G1); no query or requested-source echo either.

    `by_source` lists every source inside the grant: earliest / latest event time and whether it has
    any event in the requested time range (overall when no range was given). A source the grant
    explicitly names but that has no events is listed with null times and has_events_in_range=false,
    so a host can tell "nothing recorded" from "not searched". Deliberately NO count of sources or
    events excluded by the grant: it would reveal that out-of-grant data exists (breaks G1)."""
    coverage: dict[str, Any] = {
        "partial": False,
        "note": "Searched indexed history (source plugins and Penpal capture) within this client's granted scope.",
        "time_range": {"start": start, "end": end},
        "buckets": list(grant.buckets),
        "sources": list(grant.sources) if grant.sources is not None else None,
    }
    try:
        spans = source_coverage(
            buckets=list(grant.buckets), sources=list(grant.sources) if grant.sources is not None else None,
            since=start, until=end, event_type=SEARCHABLE_EVENT_TYPES,
            include_penpal_echoes=True,  # recorded-data span: the echo filter would defeat the covering index
        )
    except sqlite3.Error:
        coverage["by_source"] = None  # unknown; never an error text
        return coverage
    if grant.sources is not None:
        seen = {s["source"] for s in spans}
        spans += [{"source": n, "earliest": None, "latest": None, "has_events_in_range": False}
                  for n in grant.sources if n not in seen]
        spans.sort(key=lambda s: s["source"])
    coverage["by_source"] = spans
    # Sources inside the grant that could not be read right now (issue #22): they were NOT searched.
    # Only names + fixed reason codes; sources outside the grant are never probed into the response.
    try:
        unavailable_now = unavailable_sources(list(grant.sources) if grant.sources is not None else None)
    except Exception:  # pragma: no cover - defensive: availability must never break a search
        unavailable_now = None
    coverage["unavailable_sources"] = unavailable_now
    if unavailable_now:
        coverage["note"] += (
            " Sources listed in unavailable_sources could not be read (reason given) and were NOT searched: "
            "say that instead of claiming nothing exists in them."
        )
    return coverage


def _record_returned(grant: Grant, payload: dict[str, Any], query: Optional[str]) -> None:
    """Write the ids this response handed to the client into the feedback ledger (rate_result checks
    against it). Reads the response we just built, never the audit log."""
    items: list[tuple[int, Optional[int]]] = []
    for r in payload.get("results") or []:
        event = feedback.parse_ref(r.get("id"))
        if event is None or event[0] != "evt":
            continue
        evidence = feedback.parse_ref(r.get("evidence_id"))
        items.append((event[1], evidence[1] if evidence and evidence[0] == "evd" else None))
    feedback.record_returned(grant.client_id, items, query)


def search_context(
    token: Optional[str],
    keywords: Any,
    time_range: Any = None,
    sources: Any = None,
    limit: Any = 10,
    include_penpal_echoes: Any = False,
    *,
    client: Optional[str] = None,
) -> dict[str, Any]:
    decision = check_access(token, client, "read")
    if decision.grant is None:
        audit.record(client=decision.client, tool=TOOL_SEARCH_CONTEXT, allowed=False,
                     reason_code=decision.reason, scope=decision.scope)
        return permission_denied()
    payload = _redact_payload(_search(decision.grant, keywords, time_range, sources, limit, include_penpal_echoes))
    count = len(payload.get("results") or [])
    if payload.get("status") == "ok":
        _record_returned(decision.grant, payload, ", ".join(k.strip() for k in keywords))
    audit.record(client=decision.client, tool=TOOL_SEARCH_CONTEXT, allowed=True,
                 reason_code=payload.get("status", "error"), scope=decision.scope, result_count=count)
    return payload


def _search(grant: Grant, keywords: Any, time_range: Any, sources: Any, limit: Any,
            include_echoes: Any = False) -> dict[str, Any]:

    # Fixed-text validation: caller-supplied values are never echoed back.
    if sources is not None and not (isinstance(sources, list) and all(isinstance(x, str) for x in sources)):
        return {"status": "invalid_argument", "message": "sources must be a list of source names"}
    if isinstance(limit, bool) or not isinstance(limit, int):
        return {"status": "invalid_argument", "message": "limit must be an integer between 1 and 100"}
    if not isinstance(include_echoes, bool):
        return {"status": "invalid_argument", "message": "include_penpal_echoes must be a boolean"}

    try:
        start, end = _parse_time_range(time_range)
    except (ValueError, TypeError):
        # Fixed text: never echo caller-supplied strings (or parser internals) back.
        return {"status": "invalid_argument", "message": "time_range must be an object with optional ISO 'start' and 'end'"}
    if (
        not isinstance(keywords, list)
        or not 1 <= len(keywords) <= MAX_KEYWORDS
        or not all(isinstance(k, str) and k.strip() for k in keywords)
    ):
        return {"status": "invalid_argument", "message": KEYWORDS_MESSAGE}
    if any(_looks_like_sentence(k.strip()) for k in keywords):
        return {"status": "invalid_argument", "message": SENTENCE_KEYWORD_MESSAGE}

    # Scope is enforced inside the SQL WHERE (before LIMIT). A requested source outside the grant
    # simply matches nothing: same answer as a source that does not exist.
    effective_sources = sources
    if grant.sources is not None:
        effective_sources = list(grant.sources) if sources is None else [x for x in sources if x in grant.sources]

    scope = dict(
        since=start, until=end, sources=effective_sources, buckets=list(grant.buckets),
        event_type=SEARCHABLE_EVENT_TYPES, include_penpal_echoes=include_echoes,
    )
    # One feedback entry per normalised keyword, reported by its position in the caller's list (first
    # occurrence). The caller's text is never echoed back (B1 / G1), so the host maps index -> its own word.
    terms = normalize_keywords(keywords)
    first_index: dict[str, int] = {}  # case-insensitive, as normalize_keywords de-duplicates
    for index, raw in enumerate(keywords):
        for norm in normalize_keywords([raw]):
            first_index.setdefault(norm.lower(), index)
    try:
        hits = search_events(keywords, limit=limit, **scope)
        in_scope = {term: keyword_in_scope(term, **scope) for term in terms}
    except sqlite3.Error:
        # Never surface SQLite / FTS error text: it can echo query fragments or index internals.
        return {"status": "error", "message": "search failed"}
    coverage = _coverage(grant, start, end)
    in_results = {term: sum(1 for h in hits if term in h.get("matched_terms", ())) for term in terms}
    keyword_feedback = [
        {"index": first_index[term.lower()], "in_scope": in_scope[term], "in_results": in_results[term]}
        for term in terms
    ]
    if not hits:
        time_range_given = start is not None or end is not None
        hint = _no_results_hints(keyword_feedback, time_range_given, effective_sources, coverage)
        return {"status": "no_results", "results": [], "coverage": coverage, "keywords": keyword_feedback,
                "hint": hint}
    availability = refresh_availability(
        [(h["evidence_id"], h["evidence_locator"], h.get("evidence_line"), h.get("evidence_stored_available", 1))
         for h in hits if h["evidence_id"] is not None and h.get("evidence_locator")],
        deep=False,
    )
    results = [
        {
            "id": f"evt_{h['id']}",
            "snippet": h["snippet"],
            "occurred_at": h["occurred_at"],
            "source": h["source"],
            "bucket": h["bucket"],
            "evidence_id": f"evd_{h['evidence_id']}" if h["evidence_id"] is not None else None,
            # Identical records (same text, source family, bucket) are folded into this one hit; the
            # counts and range cover in-scope rows only (the fold runs after the scope filter).
            "occurrences": h.get("occurrences", 1),
            "first_seen": h.get("first_seen", h["occurred_at"]),
            "last_seen": h.get("last_seen", h["occurred_at"]),
            # Other sources that recorded the same browser page (folded into this hit); [] when none.
            "also_seen_in": list(h.get("also_seen_in", [])),
            # Cheap path-existence check only; the line / commit is verified by get_evidence.
            "evidence_available": bool(availability.get(h["evidence_id"], False)),
            # Two tiers (PRD 3.7.3): "strong" = several keywords co-occur (or the only keyword hit);
            # "weak" = a possible clue. Derived from the caller's own keywords and this one document.
            "match": "strong" if h["matched"] >= 2 or h["keyword_count"] == 1 else "weak",
            # Fixed format, no caller text echoed.
            "match_reason": f"{_MATCH_REASON}: {h['matched']} of {h['keyword_count']} keywords",
        }
        for h in hits
    ]
    return {"status": "ok", "results": results, "coverage": coverage, "keywords": keyword_feedback}


# Fixed next-step hints on no_results (a closed enum: never built from caller text).
HINT_SPLIT_OR_REPHRASE = "split_or_rephrase"
HINT_BROWSE_TIMELINE = "browse_timeline"


def _no_results_hints(keyword_feedback: list[dict[str, Any]], time_range_given: bool,
                      effective_sources: Optional[list[str]], coverage: dict[str, Any]) -> list[str]:
    """What the host should do next after an empty search. split_or_rephrase: no keyword matched
    anything in the granted scope. browse_timeline: a time range was given and a requested (or
    granted) source has events inside it, so get_timeline for that period can show them."""
    hints: list[str] = []
    if keyword_feedback and not any(k["in_scope"] for k in keyword_feedback):
        hints.append(HINT_SPLIT_OR_REPHRASE)
    spans = coverage.get("by_source") or []
    if time_range_given and any(
        s["has_events_in_range"] and (effective_sources is None or s["source"] in effective_sources)
        for s in spans
    ):
        hints.append(HINT_BROWSE_TIMELINE)
    return hints


def _gate(token: Optional[str], client: Optional[str], tool: str):
    decision = check_access(token, client, "read")
    if decision.grant is None:
        audit.record(client=decision.client, tool=tool, allowed=False,
                     reason_code=decision.reason, scope=decision.scope)
        return decision, permission_denied()
    return decision, None


def _parse_evidence_id(raw: str) -> Optional[int]:
    body = raw[4:]
    if raw.startswith("evd_") and body.isascii() and body.isdigit() and len(body) < 19:
        return int(body)
    return None


def _scope_sources(grant: Grant, sources: Optional[list[str]]) -> Optional[list[str]]:
    if grant.sources is None:
        return sources
    return list(grant.sources) if sources is None else [x for x in sources if x in grant.sources]


def get_evidence(token: Optional[str], evidence_id: Any, *, client: Optional[str] = None) -> dict[str, Any]:
    decision, denied = _gate(token, client, TOOL_GET_EVIDENCE)
    if denied is not None:
        return denied
    payload = _redact_payload(_get_evidence(decision.grant, evidence_id))
    if payload.get("status") in ("ok", "source_unavailable", "not_resolvable") and "evidence" in payload:
        ev = payload["evidence"]
        _record_returned(decision.grant, {"results": [{"id": ev.get("event_id"), "evidence_id": ev.get("id")}]}, None)
    audit.record(client=decision.client, tool=TOOL_GET_EVIDENCE, allowed=True,
                 reason_code=payload.get("status", "error"), scope=decision.scope,
                 result_count=1 if "evidence" in payload else 0)
    return payload


def _get_evidence(grant: Grant, evidence_id: Any) -> dict[str, Any]:
    if not isinstance(evidence_id, str):
        return {"status": "invalid_argument", "message": "evidence_id must be a string such as evd_123"}
    # Malformed, nonexistent and out-of-scope ids all get the identical not_found body.
    numeric = _parse_evidence_id(evidence_id)
    if numeric is None:
        return dict(EVIDENCE_NOT_FOUND)
    try:
        row = scoped_evidence(numeric, sources=_scope_sources(grant, None), buckets=list(grant.buckets),
                              event_type=SEARCHABLE_EVENT_TYPES)
    except sqlite3.Error:
        return {"status": "error", "message": "evidence lookup failed"}
    if row is None:
        return dict(EVIDENCE_NOT_FOUND)
    available = refresh_availability(
        [(row["evidence_id"], row["locator"], row["line"], row["stored_available"])], deep=True
    )[row["evidence_id"]]
    evidence: dict[str, Any] = {
        "id": f"evd_{row['evidence_id']}",
        "event_id": f"evt_{row['event_id']}",
        "source": row["source"],
        "bucket": row["bucket"],
        "occurred_at": row["occurred_at"],
        # Stored, already-redacted text only. The original file is never read back out.
        "excerpt": " ".join((row["excerpt_text"] or "").split())[:EXCERPT_CHARS],
        "available": available,
        "locator": None,
    }
    if is_unresolvable_locator(row["locator"]):
        reason = NOT_RESOLVABLE_MESSAGES.get(unresolvable_source(row["locator"]), NOT_RESOLVABLE_DEFAULT)
        return {"status": "not_resolvable", "message": reason, "evidence": evidence}
    original = None
    if available and row["source"] in ORIGINAL_SOURCES:
        original = read_original(
            row["source"],
            row["locator"],
            is_blocked_sqlite=is_blocked_sqlite,
            is_excluded_path=is_excluded_path,
        )
        if original is None:  # the file / visit row is gone although the file path still exists
            available = evidence["available"] = False
            _store_unavailable(row["evidence_id"])
    if not available:
        # AT21: keep the stored excerpt, give no path / commit that could look openable.
        return {"status": "source_unavailable", "message": SOURCE_UNAVAILABLE_MESSAGE, "evidence": evidence}
    parsed = split_git_locator(row["locator"])
    if is_penpal_locator(row["locator"]):
        # Penpal is the original record: point at the event itself, always available.
        evidence["locator"] = {"type": "penpal", "event_id": f"evt_{row['event_id']}"}
    elif parsed is not None:
        evidence["locator"] = {"type": "git", "repo": parsed[0], "commit": parsed[1]}
    elif split_browser_locator(row["locator"]) is not None:
        db_path, visit_id = split_browser_locator(row["locator"])
        evidence["locator"] = {"type": "browser_visit", "db": db_path, "visit_id": visit_id}
    else:
        evidence["locator"] = {"type": "file", "path": row["locator"], "line": row["line"]}
    if original is not None:
        evidence["original"] = original
    return {"status": "ok", "evidence": evidence}


def _store_unavailable(evidence_id: int) -> None:
    try:
        conn = get_conn()
        conn.execute("UPDATE evidence SET available = 0 WHERE id = ?", (evidence_id,))
        conn.commit()
    except sqlite3.Error:
        pass


def _parse_cursor(raw: Any) -> tuple[str, int]:
    if not isinstance(raw, str) or len(raw) > 80:
        raise ValueError("cursor")
    ts, _, ident = raw.rpartition("|")
    if not ts or not ident.isascii() or not ident.isdigit():
        raise ValueError("cursor")
    parsed = parse_time_bound(ts)
    if parsed is None:
        raise ValueError("cursor")
    return ts, int(ident)  # raw: it was issued by us from a stored occurred_at


def get_timeline(
    token: Optional[str],
    start: Any,
    end: Any,
    sources: Any = None,
    limit: Any = TIMELINE_DEFAULT_LIMIT,
    cursor: Any = None,
    include_penpal_echoes: Any = False,
    *,
    client: Optional[str] = None,
) -> dict[str, Any]:
    decision, denied = _gate(token, client, TOOL_GET_TIMELINE)
    if denied is not None:
        return denied
    payload = _redact_payload(_get_timeline(decision.grant, start, end, sources, limit, cursor, include_penpal_echoes))
    if payload.get("status") == "ok":
        span = (payload.get("coverage") or {}).get("time_range") or {}
        _record_returned(decision.grant, payload, f"timeline {span.get('start')} .. {span.get('end')}")
    audit.record(client=decision.client, tool=TOOL_GET_TIMELINE, allowed=True,
                 reason_code=payload.get("status", "error"), scope=decision.scope,
                 result_count=len(payload.get("results") or []))
    return payload


def _get_timeline(grant: Grant, start: Any, end: Any, sources: Any, limit: Any, cursor: Any,
                  include_echoes: Any = False) -> dict[str, Any]:
    if sources is not None and not (isinstance(sources, list) and all(isinstance(x, str) for x in sources)):
        return {"status": "invalid_argument", "message": "sources must be a list of source names"}
    if isinstance(limit, bool) or not isinstance(limit, int):
        return {"status": "invalid_argument", "message": f"limit must be an integer between 1 and {TIMELINE_MAX_LIMIT}"}
    if not isinstance(include_echoes, bool):
        return {"status": "invalid_argument", "message": "include_penpal_echoes must be a boolean"}
    bad_range = {"status": "invalid_argument",
                 "message": "start and end are required ISO dates or datetimes with start <= end; cursor must come from a previous call"}
    if not isinstance(start, str) or not isinstance(end, str):
        return bad_range
    try:
        since, until = _parse_time_range({"start": start, "end": end})
        after = _parse_cursor(cursor) if cursor is not None else None
    except (ValueError, TypeError):
        return bad_range
    if since is None or until is None or since > until:
        return bad_range
    try:
        rows, has_more = timeline_events(
            since=since, until=until, sources=_scope_sources(grant, sources), buckets=list(grant.buckets),
            after=after, event_type=SEARCHABLE_EVENT_TYPES, limit=limit,
            include_penpal_echoes=include_echoes,
        )
    except sqlite3.Error:
        return {"status": "error", "message": "timeline failed"}
    coverage = _coverage(grant, since, until)
    if not rows:
        return {"status": "no_results", "results": [], "has_more": False, "coverage": coverage}
    results = [
        {
            "id": f"evt_{r['id']}",
            "occurred_at": r["occurred_at"],
            "source": r["source"],
            "bucket": r["bucket"],
            "snippet": r["snippet"],
            "evidence_id": f"evd_{r['evidence_id']}" if r["evidence_id"] is not None else None,
        }
        for r in rows
    ]
    payload: dict[str, Any] = {"status": "ok", "results": results, "has_more": has_more, "coverage": coverage}
    if has_more:
        payload["next_cursor"] = f"{rows[-1]['occurred_at']}|{rows[-1]['id']}"
    return payload


RATE_NOT_FOUND = {"status": "not_found", "message": "No result with this id was returned to this client."}
RATE_ARG_MESSAGE = "verdict must be correct, wrong or missing; correct/wrong need result_id; missing needs result_id or query"


def rate_result(
    token: Optional[str],
    result_id: Any,
    verdict: Any,
    note: Any = None,
    query: Any = None,
    *,
    client: Optional[str] = None,
) -> dict[str, Any]:
    """Record whether a result this client was shown was right. Needs only a read grant. Every id that
    this client was not returned (unknown, malformed, other client's, expired, out of scope) gets the
    identical not_found body, so the tool cannot be used to probe what exists."""
    decision, denied = _gate(token, client, TOOL_RATE_RESULT)
    if denied is not None:
        return denied
    payload = _rate_result(decision.grant, result_id, verdict, note, query)
    audit.record(client=decision.client, tool=TOOL_RATE_RESULT, allowed=True,
                 reason_code=payload.get("status", "error"), scope=decision.scope)
    return payload


def _rate_result(grant: Grant, result_id: Any, verdict: Any, note: Any, query: Any) -> dict[str, Any]:
    bad = {"status": "invalid_argument", "message": RATE_ARG_MESSAGE}
    if verdict not in feedback.VERDICTS or not isinstance(verdict, str):
        return bad
    if (result_id is not None and not isinstance(result_id, str)) or \
            (note is not None and not isinstance(note, str)) or (query is not None and not isinstance(query, str)):
        return bad
    stored_query = feedback.clean_text(query, feedback.MAX_QUERY_CHARS)
    if result_id is None and (verdict != "missing" or stored_query is None):
        return bad
    event_id = evidence_id = None
    ledger_query = None
    if result_id is not None:
        row = feedback.lookup_returned(
            grant.client_id, result_id, buckets=list(grant.buckets),
            sources=list(grant.sources) if grant.sources is not None else None)
        if row is None:
            return dict(RATE_NOT_FOUND)
        event_id, evidence_id, ledger_query = row["event_id"], row["evidence_id"], row["query"]
    try:
        stored = feedback.save_feedback(
            grant.client_id, verdict, buckets=list(grant.buckets), event_id=event_id, evidence_id=evidence_id,
            query=stored_query or ledger_query, note=note)
    except sqlite3.Error:
        return {"status": "error", "message": "feedback could not be saved"}
    if not stored:
        return {"status": "rate_limited", "message": "Too much feedback stored for this client; try again later."}
    return {"status": "ok", "recorded": True}
