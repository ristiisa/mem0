"""Remote MCP endpoint (streamable HTTP, JSON responses, stateless) for
claude.ai custom connectors and other remote MCP clients.

Authenticates with a static ``Authorization: Bearer <MEM0_MCP_TOKEN>`` header
and scopes every call to ``MEM0_MCP_USER_ID`` so it shares memories with the
Claude Code plugin configured with the same user id.
"""

import logging
import os
import secrets
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, Response
from server_state import get_memory_instance

logger = logging.getLogger(__name__)

router = APIRouter(tags=["mcp"])

MCP_TOKEN = os.environ.get("MEM0_MCP_TOKEN", "")
MCP_USER_ID = os.environ.get("MEM0_MCP_USER_ID", "")
MCP_APP_ID = os.environ.get("MEM0_MCP_APP_ID", "claude-ai")
PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "search_memories",
        "description": "Search the user's long-term memory (facts, preferences, project and infrastructure notes saved from Claude Code and Claude apps). Use before answering questions that may depend on past context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "add_memory",
        "description": "Save something to the user's long-term memory. Pass the information as plain text; it is distilled into durable facts.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "What to remember."}},
            "required": ["text"],
        },
    },
    {
        "name": "list_memories",
        "description": "List the user's stored memories, most recent first.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}},
        },
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "delete_memory",
        "description": "Delete one memory by its id (ids are shown by search_memories and list_memories).",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
        "annotations": {"destructiveHint": True, "idempotentHint": True},
    },
]


def _authorized(request: Request) -> bool:
    scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
    return bool(MCP_TOKEN) and scheme.lower() == "bearer" and secrets.compare_digest(value.strip(), MCP_TOKEN)


def _format(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "No memories found."
    lines = []
    for item in items:
        score = f"{item['score']:.2f} " if item.get("score") is not None else ""
        app_id = (item.get("metadata") or {}).get("app_id") or item.get("app_id") or ""
        created = (item.get("created_at") or "")[:10]
        lines.append(f"- {score}{item.get('memory')}  [id={item.get('id')} app={app_id} {created}]")
    return "\n".join(lines)


def _int_arg(args: Dict[str, Any], key: str, default: int, upper: int) -> int:
    try:
        return max(1, min(int(args.get(key) or default), upper))
    except (TypeError, ValueError):
        return default


def _call_tool(name: str, args: Dict[str, Any]) -> str:
    memory = get_memory_instance()
    if name == "search_memories":
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query is required")
        response = memory.search(query=query, filters={"user_id": MCP_USER_ID}, top_k=_int_arg(args, "limit", 10, 50))
        return _format(response.get("results", []))
    if name == "add_memory":
        text = str(args.get("text") or "").strip()
        if not text:
            raise ValueError("text is required")
        response = memory.add(
            messages=[{"role": "user", "content": text}],
            user_id=MCP_USER_ID,
            metadata={"app_id": MCP_APP_ID, "mem0_source": "mcp"},
        )
        results = response.get("results", []) if isinstance(response, dict) else []
        if not results:
            return "Nothing new to store (already known or not a durable fact)."
        return "\n".join(f"- {r.get('event')}: {r.get('memory')}  [id={r.get('id')}]" for r in results)
    if name == "list_memories":
        limit = _int_arg(args, "limit", 50, 200)
        response = memory.get_all(filters={"user_id": MCP_USER_ID}, top_k=limit)
        items = response.get("results", []) if isinstance(response, dict) else (response or [])
        items = sorted(items, key=lambda m: m.get("created_at") or "", reverse=True)[:limit]
        return _format(items)
    if name == "delete_memory":
        memory_id = str(args.get("memory_id") or "").strip()
        if not memory_id:
            raise ValueError("memory_id is required")
        if (memory.get(memory_id) or {}).get("user_id") != MCP_USER_ID:
            raise ValueError("memory not found")
        memory.delete(memory_id=memory_id)
        return f"Deleted {memory_id}."
    raise LookupError(f"Unknown tool: {name}")


def _handle(message: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mem0-self-hosted", "version": "1.0.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            text, is_error = _call_tool(params.get("name"), params.get("arguments") or {}), False
        except (ValueError, LookupError) as exc:
            text, is_error = str(exc), True
        except Exception as exc:
            logger.warning("mcp tool %s failed: %s", params.get("name"), str(exc)[:300])
            text, is_error = "Memory operation failed.", True
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    elif request_id is None:
        return None
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


@router.post("/mcp")
def mcp_post(request: Request, payload: Any = Body(None)):
    if not _authorized(request):
        return JSONResponse(status_code=401, content={"error": "unauthorized"}, headers={"WWW-Authenticate": "Bearer"})
    if isinstance(payload, list):
        replies = [reply for reply in (_handle(m) for m in payload) if reply is not None]
        return JSONResponse(content=replies) if replies else Response(status_code=202)
    reply = _handle(payload)
    return JSONResponse(content=reply) if reply is not None else Response(status_code=202)


@router.api_route("/mcp", methods=["GET", "DELETE"])
def mcp_no_stream():
    return Response(status_code=405, headers={"Allow": "POST"})
