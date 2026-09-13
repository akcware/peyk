"""The public web for the chat agent: a search and a page read, both executed by the workers (the agent asks
through intents and is re-run with the results, like document queries). One protocol (`WebSearcher`), two
engines behind it:

- `DuckDuckGo` (default): the HTML endpoint, no key, no account. Good enough for "what is MHP", "who makes
  this transponder"; it may throttle a burst of requests.
- `Tavily`: an API built for agents (needs TAVILY_API_KEY); returns page content with the results.

`open()` fetches a page and reduces it to readable text; only public http(s) hosts, never the local network."""
from __future__ import annotations

import html
import ipaddress
import re
from html.parser import HTMLParser
from typing import Any, ClassVar, Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from core.log import get_logger

log = get_logger("web")

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36 Peyk/0.1"
MAX_RESULTS = 6
MAX_PAGE_CHARS = 6000
MAX_DOWNLOAD = 2_000_000
TIMEOUT = httpx.Timeout(15.0, connect=8.0)

_DDG_RESULT = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_DDG_SNIPPET = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


class WebSearcher(Protocol):
    async def search(self, query: str, *, max_results: int = MAX_RESULTS) -> list[dict[str, Any]]: ...

    async def open(self, url: str, *, max_chars: int = MAX_PAGE_CHARS) -> dict[str, Any]: ...


def strip_tags(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub("", fragment)).split())


def ddg_target(href: str) -> str:
    """DuckDuckGo wraps result links: //duckduckgo.com/l/?uddg=<url-encoded target>&rut=…"""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target or href
    return href


def parse_ddg_html(page: str, *, max_results: int = MAX_RESULTS) -> list[dict[str, Any]]:
    links = _DDG_RESULT.findall(page)
    snippets = [strip_tags(s) for s in _DDG_SNIPPET.findall(page)]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, (href, title_html) in enumerate(links):
        url = ddg_target(html.unescape(href))
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        out.append({"title": strip_tags(title_html), "url": url, "snippet": snippets[i] if i < len(snippets) else ""})
        if len(out) >= max_results:
            break
    return out


class _TextExtractor(HTMLParser):
    SKIP: ClassVar[set[str]] = {"script", "style", "noscript", "svg", "head", "nav", "footer", "iframe", "template"}
    BLOCK: ClassVar[set[str]] = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = " ".join(data.split())
        if not self._skip:
            self.parts.append(data)


def html_to_text(page: str, *, max_chars: int = MAX_PAGE_CHARS) -> tuple[str, str]:
    """(title, readable text): scripts, styles and navigation dropped, whitespace folded, capped."""
    p = _TextExtractor()
    try:
        p.feed(page)
    except Exception as e:  # noqa: BLE001 - a broken page still yields what was parsed
        log.warning("web.parse_failed", error=str(e)[:120])
    lines = [" ".join(line.split()) for line in "".join(p.parts).splitlines()]
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return p.title, text


def public_http_url(url: str) -> str:
    """Only http(s) to a public host. The workers run next to the database; a page read must never become a
    request into the local network."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("only public http(s) links can be opened")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ValueError("that address is not public")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url.strip()
    if not ip.is_global:
        raise ValueError("that address is not public")
    return url.strip()


class _Fetcher:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True)
        return self._http

    HEADERS: ClassVar[dict[str, str]] = {"User-Agent": USER_AGENT, "Accept-Language": "en,de;q=0.8,tr;q=0.7"}

    async def open(self, url: str, *, max_chars: int = MAX_PAGE_CHARS) -> dict[str, Any]:
        url = public_http_url(url)
        resp = await self.http.get(url, headers={**self.HEADERS, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"})
        resp.raise_for_status()
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        body = resp.content[:MAX_DOWNLOAD].decode(resp.encoding or "utf-8", errors="replace")
        if ctype.startswith("text/html") or ctype in ("application/xhtml+xml", ""):
            title, text = html_to_text(body, max_chars=max_chars)
        elif ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
            title, text = "", body[:max_chars]
        else:
            raise ValueError(f"not a readable page ({ctype})")
        log.info("web.opened", url=str(resp.url)[:200], status=resp.status_code, chars=len(text))
        return {"url": str(resp.url), "title": title, "text": text}


class DuckDuckGo(_Fetcher):
    ENDPOINT = "https://html.duckduckgo.com/html/"

    async def search(self, query: str, *, max_results: int = MAX_RESULTS) -> list[dict[str, Any]]:
        resp = await self.http.post(self.ENDPOINT, data={"q": query, "kl": "wt-wt"}, headers=self.HEADERS)
        resp.raise_for_status()
        results = parse_ddg_html(resp.text, max_results=max_results)
        log.info("web.searched", engine="duckduckgo", query=query[:120], results=len(results))
        return results


class Tavily(_Fetcher):
    ENDPOINT = "https://api.tavily.com/search"

    def __init__(self, api_key: str, http: httpx.AsyncClient | None = None) -> None:
        super().__init__(http)
        self._key = api_key

    async def search(self, query: str, *, max_results: int = MAX_RESULTS) -> list[dict[str, Any]]:
        resp = await self.http.post(self.ENDPOINT, headers=self.HEADERS,
                                    json={"api_key": self._key, "query": query, "max_results": max_results, "include_answer": False})
        resp.raise_for_status()
        results = [{"title": r.get("title") or "", "url": r.get("url") or "", "snippet": (r.get("content") or "")[:400]}
                   for r in (resp.json().get("results") or [])][:max_results]
        log.info("web.searched", engine="tavily", query=query[:120], results=len(results))
        return results


def make_web_searcher(engine: str, *, tavily_api_key: str = "", http: httpx.AsyncClient | None = None) -> WebSearcher | None:
    if engine == "off":
        return None
    if engine == "tavily":
        if not tavily_api_key:
            raise RuntimeError("WEB_SEARCH_ENGINE=tavily needs TAVILY_API_KEY")
        return Tavily(tavily_api_key, http)
    return DuckDuckGo(http)
