"""Validate, scope and persist distilled memory without model calls."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

from penpal.foundation.desensitizer import desensitize, redact_secrets
from penpal.foundation.project import normalize_project
from penpal.search.engine import scoped_evidence
from penpal.sources.recipes.catalog import load_catalog
from penpal.store.buckets import default_bucket_for_source, normalize_bucket
from penpal.store.capture_events import CAPTURE_SEARCHABLE_EVENT_TYPES


class Denied(ValueError):
    pass


def source_default_bucket(client_id: str) -> str | None:
    tool = next((recipe for recipe in load_catalog() if recipe.id == client_id), None)
    if tool is None:
        return None
    return default_bucket_for_source(tool.adapter or "ai_tools")


def _evidence(raw, grant):
    if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
        raise ValueError("evidence_ids must be a list of evidence IDs")
    ids, buckets = [], set()
    for value in raw:
        body = value[4:] if value.startswith("evd_") else ""
        if not body.isascii() or not body.isdigit() or len(body) >= 19:
            raise Denied()
        row = scoped_evidence(int(body), buckets=list(grant.buckets),
                              sources=list(grant.sources) if grant.sources is not None else None,
                              event_type=("semantic_event", *CAPTURE_SEARCHABLE_EVENT_TYPES))
        if row is None:
            raise Denied()
        ids.append(f"evd_{int(body)}")
        buckets.add(row["bucket"])
    return sorted(set(ids)), buckets


def visible(row, grant):
    try:
        ids = json.loads(row["evidence_ids"])
        buckets = set(json.loads(row["buckets"]))
        if not buckets or not buckets.issubset(grant.buckets):
            return False
        if ids:
            _evidence(ids, grant)
        elif grant.sources is not None and "memory" not in grant.sources:
            return False
        return True
    except (ValueError, TypeError):
        return False


def _target(conn, raw, grant):
    if not isinstance(raw, str) or not raw.startswith("mem_") or not raw[4:].isascii() or not raw[4:].isdigit() or len(raw[4:]) >= 19:
        raise Denied()
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (int(raw[4:]),)).fetchone()
    if row is None or not visible(row, grant):
        raise Denied()
    return row


def commit_one(conn, item, grant):
    if not isinstance(item, dict):
        raise ValueError("each item must be an object")
    kind, content, scope = item.get("type"), item.get("content"), item.get("scope")
    if kind not in ("fact", "preference", "decision", "procedure") or scope not in ("global", "project"):
        raise ValueError("invalid type or scope")
    if not isinstance(content, str) or not content.strip() or '\n' in content or '\r' in content:
        raise ValueError("content must be one non-empty line")
    if redact_secrets(content) != content:
        raise ValueError("content contains a secret")
    content = desensitize(content.strip())
    project = normalize_project(item.get("project"))
    if scope == "project" and not project:
        raise ValueError("project is required for project scope; the host must supply its project")
    if scope == "global":
        project = None
    confidence = item.get("confidence", 1.0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    ids, buckets = _evidence(item.get("evidence_ids", []), grant)
    if not ids:
        if grant.sources is not None and "memory" not in grant.sources:
            raise Denied()
        if item.get("bucket") is not None:
            try:
                bucket = normalize_bucket(item["bucket"])
            except (ValueError, AttributeError):
                raise ValueError("invalid bucket") from None
        else:
            bucket = source_default_bucket(grant.client_id)
            if bucket is None:
                if len(grant.buckets) != 1:
                    raise ValueError(f"bucket is required; available buckets: {', '.join(sorted(grant.buckets))}")
                bucket = grant.buckets[0]
        if bucket not in grant.buckets:
            raise Denied()
        buckets = {bucket}
    old = _target(conn, item["supersedes"], grant) if item.get("supersedes") is not None else None
    if old is not None and (old["status"] != "active" or old["superseded_by"] is not None):
        raise ValueError("memory is not active")
    normalized = " ".join(content.casefold().split())
    digest = hashlib.sha256(json.dumps([kind, scope, project, normalized], ensure_ascii=False).encode()).hexdigest()
    candidates = conn.execute("SELECT * FROM memories WHERE content_hash = ? AND status = 'active' AND superseded_by IS NULL", (digest,)).fetchall()
    matching = [r for r in candidates if visible(r, grant)]
    duplicate = matching[0] if matching else None
    if duplicate is not None:
        if old is not None and old["id"] == duplicate["id"]:
            raise ValueError("memory cannot supersede itself")
        merged_ids = sorted(set(json.loads(duplicate["evidence_ids"])) | set(ids))
        merged_buckets = sorted(set(json.loads(duplicate["buckets"])) | buckets)
        # An unbacked memory is a distinct security provenance; never merge it with evidence-backed content.
        # A reader must cover every bucket of a memory, so merging in a new bucket would hide the memory
        # from readers who could see it before; such a duplicate becomes its own row instead.
        if bool(merged_ids) != bool(ids) or bool(ids) != bool(json.loads(duplicate["evidence_ids"])):
            duplicate = None
        elif not buckets <= set(json.loads(duplicate["buckets"])):
            duplicate = None
        else:
            conn.execute("UPDATE memories SET evidence_ids = ?, buckets = ? WHERE id = ?", (json.dumps(merged_ids), json.dumps(merged_buckets), duplicate["id"]))
    if duplicate is None:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute("INSERT INTO memories(type,content,scope,project,source_tool,evidence_ids,buckets,content_hash,valid_from,status,created_at,confidence,source) VALUES (?,?,?,?,?,?,?,?,?,'active',?,?, 'host')", (kind, content, scope, project, grant.client_id, json.dumps(ids), json.dumps(sorted(buckets)), digest, now, now, confidence))
        memory_id = cur.lastrowid
    else:
        memory_id = duplicate["id"]
    if old is not None:
        conn.execute("UPDATE memories SET superseded_by = ?, status = 'superseded' WHERE id = ?", (memory_id, old["id"]))
    return {"id": f"mem_{memory_id}", "merged": duplicate is not None}
