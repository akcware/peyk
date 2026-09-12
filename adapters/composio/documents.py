"""Document surfaces over Composio: Notion, Google Drive, Google Docs. Search/read for the chat agent,
create for the approval machine. Response shapes differ per action and version, so parsing is defensive:
we look for lists under common keys and pick title/id/url/updated from the usual fields.

Slugs verified against the Composio API on 2026-09-12 (toolkit versions pinned in COMPOSIO_TOOLKIT_VERSIONS).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

ExecuteFn = Callable[[str, dict[str, Any], str | None], dict[str, Any]]

DOCUMENT_SERVICES = ("notion", "googledrive", "googledocs")
MAX_TEXT = 6000


def _data(resp: dict[str, Any]) -> Any:
    if not resp.get("successful", True):
        raise RuntimeError(str(resp.get("error") or "composio action failed"))
    return resp.get("data") or {}


def _items(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for k in keys:
        v = data.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    for v in data.values():   # last resort: first list of dicts anywhere at top level
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return []


def _notion_title(page: dict[str, Any]) -> str:
    props = page.get("properties") or {}
    for v in props.values():
        if isinstance(v, dict) and v.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in v.get("title") or []) or "(untitled)"
    for k in ("title", "name"):
        t = page.get(k)
        if isinstance(t, str) and t:
            return t
        if isinstance(t, list):
            return "".join(x.get("plain_text", "") for x in t if isinstance(x, dict)) or "(untitled)"
    return "(untitled)"


def _first(d: dict[str, Any], *keys: str, default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return default


# ---------------- search ----------------

def search_notion(execute: ExecuteFn, uid: str | None, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    data = _data(execute("NOTION_SEARCH_NOTION_PAGE", {"query": query, "page_size": limit, "filter_value": "page", "filter_property": "object"}, uid))
    out = []
    for p in _items(data, "results", "pages", "items")[:limit]:
        out.append({"service": "notion", "id": _first(p, "id"), "title": _notion_title(p), "url": _first(p, "url", "public_url"),
                    "updated": _first(p, "last_edited_time", "updated_at")[:10], "snippet": ""})
    return out


def search_googledrive(execute: ExecuteFn, uid: str | None, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    q = f"name contains '{query.replace(chr(39), '')}' and trashed = false"
    data = _data(execute("GOOGLEDRIVE_LIST_FILES", {"q": q, "pageSize": limit, "orderBy": "modifiedTime desc",
                                                    "fields": "files(id,name,mimeType,modifiedTime,webViewLink,owners)"}, uid))
    out = []
    for f in _items(data, "files", "items")[:limit]:
        out.append({"service": "googledrive", "id": _first(f, "id"), "title": _first(f, "name", "title", default="(untitled)"),
                    "url": _first(f, "webViewLink", "url"), "updated": _first(f, "modifiedTime")[:10],
                    "snippet": _first(f, "mimeType").rsplit(".", 1)[-1]})
    return out


def search_googledocs(execute: ExecuteFn, uid: str | None, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    data = _data(execute("GOOGLEDOCS_SEARCH_DOCUMENTS", {"query": query, "max_results": limit, "order_by": "modifiedTime desc"}, uid))
    out = []
    for f in _items(data, "documents", "files", "items", "results")[:limit]:
        out.append({"service": "googledocs", "id": _first(f, "id", "documentId", "document_id"), "title": _first(f, "name", "title", default="(untitled)"),
                    "url": _first(f, "webViewLink", "url"), "updated": _first(f, "modifiedTime", "modified_time")[:10], "snippet": ""})
    return out


SEARCHERS = {"notion": search_notion, "googledrive": search_googledrive, "googledocs": search_googledocs}


# ---------------- read ----------------

def read_notion(execute: ExecuteFn, uid: str | None, doc_id: str) -> dict[str, Any]:
    data = _data(execute("NOTION_GET_PAGE_MARKDOWN", {"page_id": doc_id}, uid))
    text = _first(data, "markdown", "content", "text") if isinstance(data, dict) else str(data)
    return {"service": "notion", "id": doc_id, "title": _first(data, "title", default="") if isinstance(data, dict) else "",
            "url": _first(data, "url") if isinstance(data, dict) else "", "text": text[:MAX_TEXT]}


def read_googledocs(execute: ExecuteFn, uid: str | None, doc_id: str) -> dict[str, Any]:
    data = _data(execute("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", {"document_id": doc_id, "include_tables": True}, uid))
    text = _first(data, "text", "plaintext", "plain_text", "content") if isinstance(data, dict) else str(data)
    return {"service": "googledocs", "id": doc_id, "title": _first(data, "title", default="") if isinstance(data, dict) else "",
            "url": f"https://docs.google.com/document/d/{doc_id}/edit", "text": text[:MAX_TEXT]}


def read_googledrive(execute: ExecuteFn, uid: str | None, doc_id: str) -> dict[str, Any]:
    """Drive files that are Google Docs read through Docs; other types return metadata only."""
    try:
        return {**read_googledocs(execute, uid, doc_id), "service": "googledrive"}
    except Exception:  # noqa: BLE001 - not a Google Doc
        return {"service": "googledrive", "id": doc_id, "title": "", "url": f"https://drive.google.com/file/d/{doc_id}/view",
                "text": "(binary or non-Docs file: content not readable here)"}


READERS = {"notion": read_notion, "googledrive": read_googledrive, "googledocs": read_googledocs}


# ---------------- create ----------------

def create_notion(execute: ExecuteFn, uid: str | None, title: str, body_markdown: str, parent: str | None) -> dict[str, Any]:
    if not parent:
        # a parent is required by the API: pick the most recently edited page the integration can see
        hits = search_notion(execute, uid, "", limit=1)
        if not hits:
            raise RuntimeError("Notion needs a parent page; none accessible")
        parent = hits[0]["id"]
    data = _data(execute("NOTION_CREATE_NOTION_PAGE", {"parent_id": parent, "title": title, "markdown": body_markdown}, uid))
    return {"id": _first(data, "id", "page_id"), "url": _first(data, "url", "public_url")}


def create_googledocs(execute: ExecuteFn, uid: str | None, title: str, body_markdown: str, parent: str | None) -> dict[str, Any]:
    data = _data(execute("GOOGLEDOCS_CREATE_DOCUMENT_MARKDOWN", {"title": title, "markdown_text": body_markdown}, uid))
    doc_id = _first(data, "documentId", "document_id", "id")
    if not doc_id and isinstance(data, dict):
        inner = data.get("document") or data.get("response_data") or {}
        doc_id = _first(inner, "documentId", "document_id", "id") if isinstance(inner, dict) else ""
    return {"id": doc_id, "url": f"https://docs.google.com/document/d/{doc_id}/edit" if doc_id else ""}


CREATORS = {"notion": create_notion, "googledocs": create_googledocs}
