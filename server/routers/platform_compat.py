"""Mem0 Platform (cloud) API compatibility layer for the self-hosted server.

Lets the STOCK, unmodified official mem0 Claude Code plugin talk to this
self-hosted server by setting the plugin's ``MEM0_API_URL`` to this host. The
plugin speaks the cloud *platform* API (``/v3/memories/add/`` + event polling,
``/v3/memories/search/``, ``/v2/memories/``, ``/v1/memories/{id}/``,
``/v1/ping/``) and authenticates with ``Authorization: Token <key>``. This
router translates those calls to the native synchronous ``Memory`` instance and
returns platform-shaped responses.

Additive and isolated: new file + one gated ``include_router`` line in main.py.
Gate it with ``PLATFORM_COMPAT=1``.

Verified against the stock plugin's
``integrations/claude-code-plugin/core/memory_core.py`` (Apache-2.0):
  - auth header ``Authorization: Token <key>``            (line 1819)
  - add -> ``response["event_id"]``                        (_event_id, 1871)
  - event -> ``response["status"]`` in {SUCCEEDED,FAILED}  (_wait_for_event, 1931)
            ``response["results"]`` counted                (_result_count, 1886)
  - search -> list OR ``response["results"]``; each item   (search_memories, 2342)
              read for id / memory|text / score / metadata (format_context, 2429)
  - v2 list -> POST body ``{"filters": ...}`` + ``?page=&page_size=``;
               items read for ``id`` and top-level ``app_id`` (_collect_memory_ids, 2529)
  - delete -> ``DELETE /v1/memories/{id}/`` any 2xx         (_delete_memory, 2556)
  - ping -> ``response["user_email"]``                      (telemetry._resolve_email, 785)
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from auth import ADMIN_API_KEY, AUTH_DISABLED, verify_api_key_hash
from db import Base, SessionLocal, engine
from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from models import APIKey
from server_state import get_memory_instance
from sqlalchemy import String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

logger = logging.getLogger(__name__)

router = APIRouter(tags=["platform-compat"])

# Platform-only fields the self-hosted core has no equivalent for. app_id is
# preserved (folded into metadata, filterable); the rest are dropped on add.
_DROPPED_ADD_FIELDS = (
    "custom_instructions",
    "agent_custom_instructions",
    "custom_categories",
)

PLATFORM_USER_EMAIL = os.environ.get("MEM0_PLATFORM_USER_EMAIL", "")
PLATFORM_TOKEN = os.environ.get("MEM0_PLATFORM_TOKEN", "")

# Event results are small and short-lived but must outlive a single request and
# be visible across uvicorn workers, so they live in the app DB, not in memory.
_EVENT_RESULTS_CAP = 50


class PlatformCompatEvent(Base):
    __tablename__ = "platform_compat_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    results_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(40), default="")


# Create only this table; the rest of the schema is owned by alembic.
PlatformCompatEvent.__table__.create(bind=engine, checkfirst=True)


def _token_from_header(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "token":
        return ""
    return value.strip()


def require_token(request: Request) -> None:
    """Validate ``Authorization: Token <key>`` against the configured key(s).

    Order: dedicated MEM0_PLATFORM_TOKEN, then ADMIN_API_KEY, then per-user
    API keys (m0sk_...), then AUTH_DISABLED for local development.
    """
    token = _token_from_header(request)
    if not token:
        if AUTH_DISABLED:
            return
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Provide 'Authorization: Token <key>'.",
        )

    import secrets

    if PLATFORM_TOKEN and secrets.compare_digest(token, PLATFORM_TOKEN):
        return
    if ADMIN_API_KEY and secrets.compare_digest(token, ADMIN_API_KEY):
        return

    prefix = token[:12] if len(token) >= 12 else token
    with SessionLocal() as db:
        candidates = (
            db.execute(select(APIKey).where(APIKey.key_prefix == prefix, APIKey.revoked_at.is_(None)))
            .scalars()
            .all()
        )
        for candidate in candidates:
            if verify_api_key_hash(token, candidate.key_hash):
                return

    if AUTH_DISABLED:
        return
    raise HTTPException(status_code=401, detail="Invalid API token.")


# ---------------------------------------------------------------------------
# Filter translation: platform filter tree -> native per-clause filters.
# ---------------------------------------------------------------------------

_ENTITY_KEYS = ("user_id", "agent_id", "run_id")


def _merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(a)
    merged.update(b)
    return merged


def _dnf(f: Any) -> List[Dict[str, Any]]:
    """Flatten a platform AND/OR filter tree into disjunctive normal form:
    a list of clauses, each a flat dict that is an AND of leaf constraints.
    NOT is unsupported by the plugin's search flow and is ignored."""
    if not isinstance(f, dict) or not f:
        return [{}]

    and_groups: List[List[Any]] = []
    or_group: Optional[List[Any]] = None
    leaf: Dict[str, Any] = {}
    for key, value in f.items():
        if key == "AND" and isinstance(value, list):
            and_groups.append(value)
        elif key == "OR" and isinstance(value, list):
            or_group = value
        elif key == "NOT":
            continue
        else:
            leaf[key] = value

    clauses: List[Dict[str, Any]] = [leaf] if leaf else [{}]

    for group in and_groups:
        combined: List[Dict[str, Any]] = [{}]
        for sub in group:
            sub_clauses = _dnf(sub)
            combined = [_merge(x, y) for x in combined for y in sub_clauses]
        clauses = [_merge(x, y) for x in clauses for y in combined]

    if or_group is not None:
        or_clauses: List[Dict[str, Any]] = []
        for sub in or_group:
            or_clauses += _dnf(sub)
        if not leaf and not and_groups:
            clauses = or_clauses
        else:
            clauses = [_merge(x, y) for x in clauses for y in or_clauses]

    return clauses


def _translate_leaf(clause: Dict[str, Any]) -> Dict[str, Any]:
    """Translate one DNF clause's keys to native payload filter keys.

    - metadata:{k:v}  -> k:v   (metadata is stored flat in the payload)
    - {"contains": x} -> {"icontains": x}  (dirs/categories are JSON in payload;
      substring match is the closest native equivalent)
    """
    native: Dict[str, Any] = {}
    for key, value in clause.items():
        if key == "metadata" and isinstance(value, dict):
            native.update(_translate_leaf(value))
            continue
        if isinstance(value, dict) and "contains" in value:
            native[key] = {"icontains": value["contains"]}
        else:
            native[key] = value
    return native


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _save_event(event_id: str, status: str, results: List[Any], error: str = "") -> None:
    with SessionLocal() as db:
        db.merge(
            PlatformCompatEvent(
                event_id=event_id,
                status=status,
                results_json=json.dumps(results, default=str),
                error=error,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        db.commit()


def _run_add(event_id: str, messages: List[Any], add_kwargs: Dict[str, Any]) -> None:
    status = "SUCCEEDED"
    results: List[Any] = []
    error = ""
    try:
        if messages:
            response = get_memory_instance().add(messages=messages, **add_kwargs)
            results = response.get("results", []) if isinstance(response, dict) else []
    except Exception as exc:  # the plugin treats FAILED as a soft failure
        status = "FAILED"
        error = str(exc)[:1000]
        logger.warning("platform_add failed: %s", error)
    _save_event(event_id, status, results, error)


@router.post("/v3/memories/add/")
def platform_add(background: BackgroundTasks, payload: Dict[str, Any] = Body(...), _auth=Depends(require_token)):
    """Queue native extraction and return an event_id for the plugin to poll, like the
    cloud platform. Extraction can outlast the plugin's 15s request timeout."""
    messages = payload.get("messages") or []
    metadata = dict(payload.get("metadata") or {})
    if payload.get("app_id"):
        metadata["app_id"] = payload["app_id"]
    if payload.get("source"):
        metadata.setdefault("mem0_source", payload["source"])

    add_kwargs: Dict[str, Any] = {"metadata": metadata}
    for key in _ENTITY_KEYS:
        if payload.get(key):
            add_kwargs[key] = payload[key]
    if payload.get("infer") is not None:
        add_kwargs["infer"] = bool(payload["infer"])

    event_id = uuid.uuid4().hex
    _save_event(event_id, "PROCESSING", [])
    background.add_task(_run_add, event_id, messages, add_kwargs)

    return JSONResponse(
        content={"event_id": event_id, "status": "PROCESSING", "message": "Memory addition in progress"}
    )


@router.get("/v1/event/{event_id}/")
def platform_event(event_id: str, _auth=Depends(require_token)):
    """Return the stored extraction result in the shape _wait_for_event /
    _result_count expect: {"status": ..., "results": [...]}."""
    with SessionLocal() as db:
        row = db.get(PlatformCompatEvent, event_id)
    if row is None:
        # Not persisted yet / unknown: keep the plugin polling rather than
        # raising (a <500 HTTPError would abort its poll loop).
        return {"event_id": event_id, "status": "PROCESSING", "results": []}
    try:
        results = json.loads(row.results_json or "[]")
    except json.JSONDecodeError:
        results = []
    body: Dict[str, Any] = {"event_id": event_id, "status": row.status, "results": results}
    if row.error:
        body["error"] = row.error
    return body


@router.post("/v3/memories/search/")
def platform_search(payload: Dict[str, Any] = Body(...), _auth=Depends(require_token)):
    """Translate the platform filter tree to native per-clause searches, then
    merge, dedupe by id (max score), sort, and cap at top_k."""
    query = payload.get("query") or ""
    top_k = int(payload.get("top_k") or 10)
    threshold = payload.get("threshold")
    filters = payload.get("filters") or {}

    clauses = _dnf(filters) if filters else [{}]
    memory = get_memory_instance()
    merged: Dict[str, Dict[str, Any]] = {}
    for clause in clauses:
        native_filter = _translate_leaf(clause)
        if not any(key in native_filter for key in _ENTITY_KEYS):
            # Native search requires a top-level entity id; skip unscoped clauses.
            continue
        params: Dict[str, Any] = {"filters": native_filter, "top_k": top_k}
        if threshold is not None:
            params["threshold"] = float(threshold)
        try:
            response = memory.search(query=query, **params)
        except Exception as exc:
            logger.warning("platform_search clause failed (%s): %s", native_filter, str(exc)[:300])
            continue
        for item in response.get("results", []) if isinstance(response, dict) else []:
            mem_id = str(item.get("id", ""))
            if not mem_id:
                continue
            kept = merged.get(mem_id)
            if kept is None or (item.get("score") or 0) > (kept.get("score") or 0):
                merged[mem_id] = item

    ordered = sorted(merged.values(), key=lambda m: m.get("score") or 0, reverse=True)[:top_k]
    return {"results": ordered}


def _promote_app_id(item: Dict[str, Any]) -> Dict[str, Any]:
    """Surface app_id at the top level; the plugin's list filter reads it there."""
    if "app_id" not in item:
        app_id = (item.get("metadata") or {}).get("app_id")
        if app_id is not None:
            item = {**item, "app_id": app_id}
    return item


def _list_memories(filters: Dict[str, Any], page: int, page_size: int) -> Dict[str, Any]:
    native_filter = _translate_leaf(_dnf(filters)[0]) if filters else {}
    if not any(key in native_filter for key in _ENTITY_KEYS):
        return {"results": []}
    # get_all defaults to top_k=20; fetch enough to cover the requested page.
    response = get_memory_instance().get_all(filters=native_filter, top_k=max(page, 1) * page_size)
    results = response.get("results", []) if isinstance(response, dict) else (response or [])
    start = max(page - 1, 0) * page_size
    page_items = [_promote_app_id(dict(item)) for item in results[start : start + page_size]]
    return {"results": page_items}


@router.api_route("/v2/memories/", methods=["GET", "POST"])
async def platform_list(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    user_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    run_id: Optional[str] = None,
    _auth=Depends(require_token),
):
    """Paginated list. The plugin POSTs {"filters": {...}}; GET with query
    entity params is also accepted."""
    filters: Dict[str, Any] = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            filters = body.get("filters") or {}
    if not filters:
        filters = {k: v for k, v in {"user_id": user_id, "agent_id": agent_id, "run_id": run_id}.items() if v}
    return _list_memories(filters, page, page_size)


@router.delete("/v1/memories/{memory_id}/")
def platform_delete(memory_id: str, _auth=Depends(require_token)):
    try:
        get_memory_instance().delete(memory_id=memory_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=str(exc)[:300])
    return {"message": "Memory deleted successfully"}


@router.get("/v1/ping/")
def platform_ping(_auth=Depends(require_token)):
    """Telemetry reads user_email from this; everything else is informational."""
    return {"status": "ok", "user_email": PLATFORM_USER_EMAIL}
