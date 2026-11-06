"""
Cliente HTTP para el MCP github-local (github-project, :8081).

Solo abre el Pull Request: este MCP no expone crear ramas ni subir archivos
(manage_branches es de protección/listado), de eso se encarga integration.py con git.
El owner/repo de destino son los configurados al levantar el servidor MCP.

Variables de entorno:
    GITHUB_LOCAL_MCP_URL       URL base (default: http://localhost:8081)
    GITHUB_LOCAL_MCP_API_KEY   Bearer opcional
"""
import json
import os
import re
import threading

import requests

from mcp_projects import _parse_sse_body

_state = {"sid": None, "ready": False, "rpc": 0}
_lock = threading.Lock()


def _url() -> str:
    return os.getenv("GITHUB_LOCAL_MCP_URL", "http://localhost:8081").rstrip("/")


def _headers() -> dict:
    hdrs = {
        "Content-Type": "application/json",
        "Accept":       "application/json, text/event-stream",
    }
    key = os.getenv("GITHUB_LOCAL_MCP_API_KEY", "")
    if key:
        hdrs["Authorization"] = f"Bearer {key}"
    if _state["sid"]:
        hdrs["Mcp-Session-Id"] = _state["sid"]
    return hdrs


def _next_id() -> int:
    with _lock:
        _state["rpc"] += 1
        return _state["rpc"]


def _post(payload: dict, timeout: int) -> tuple[dict, str | None]:
    resp = requests.post(f"{_url()}/mcp", headers=_headers(), json=payload, stream=True, timeout=timeout)
    resp.raise_for_status()
    sid = resp.headers.get("Mcp-Session-Id")
    if "text/event-stream" in resp.headers.get("Content-Type", ""):
        return _parse_sse_body(resp, payload.get("id")), sid
    return (resp.json() if resp.content else {}), sid


def _ensure_session(timeout: int) -> None:
    with _lock:
        if _state["ready"]:
            return
    init = {
        "jsonrpc": "2.0",
        "id":      _next_id(),
        "method":  "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities":    {},
            "clientInfo":      {"name": "s1-crewai-devops", "version": "1.0.0"},
        },
    }
    result, sid = _post(init, timeout)
    if "error" in result:
        raise RuntimeError(f"[MCP github-local] Error en initialize: {result['error']}")
    if sid:
        _state["sid"] = sid
    try:
        _post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, timeout)
    except Exception:
        pass
    _state["ready"] = True


def _rpc(method: str, params: dict, timeout: int = 30) -> dict:
    try:
        _ensure_session(timeout)
        payload = {"jsonrpc": "2.0", "id": _next_id(), "method": method, "params": params}
        result, sid = _post(payload, timeout)
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"[MCP github-local] Error de conexión: {exc}") from exc
    if sid:
        _state["sid"] = sid
    if "error" in result:
        raise RuntimeError(f"[MCP github-local] Error RPC: {result['error']}")
    return result.get("result", {})


def list_tools() -> list[dict]:
    return _rpc("tools/list", {}).get("tools", [])


def call_tool(tool: str, params: dict) -> dict:
    """tools/call; un isError dentro del resultado se eleva como RuntimeError."""
    result  = _rpc("tools/call", {"name": tool, "arguments": params})
    content = result.get("content", [])
    text    = content[0]["text"] if content and content[0].get("type") == "text" else None

    if result.get("isError"):
        raise RuntimeError(f"[MCP github-local] Tool '{tool}' falló: {text or result}")
    if text is not None:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
    return result


def _find(node, key):
    if isinstance(node, dict):
        if node.get(key):
            return node[key]
        for value in node.values():
            found = _find(value, key)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find(value, key)
            if found:
                return found
    return None


def create_pull_request(title: str, body: str, head: str, base: str) -> dict:
    """Abre un PR head → base vía manage_prs. Retorna {"number", "url"}."""
    result = call_tool("manage_prs", {
        "action": "create",
        "title":  title,
        "body":   body,
        "head":   head,
        "base":   base,
    })
    url = _find(result, "html_url") or _find(result, "url")
    if not url and isinstance(result, dict) and isinstance(result.get("raw"), str):
        m = re.search(r"https://github\.com/\S+/pull/\d+", result["raw"])
        url = m.group(0) if m else None
    number = _find(result, "number")
    if not url and not number:
        raise RuntimeError(f"[MCP github-local] Respuesta inesperada al crear el PR: {result}")
    return {"number": number, "url": url or ""}
