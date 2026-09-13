"""The public web for chat: DuckDuckGo result parsing, page-to-text, the public-host guard, the engine factory.
No network (MockTransport); the real engines only with the llm marker."""
from __future__ import annotations

import httpx
import pytest

from core.web import (
    DuckDuckGo,
    Tavily,
    ddg_target,
    html_to_text,
    make_web_searcher,
    parse_ddg_html,
    public_http_url,
)

DDG_PAGE = """<html><body>
<div class="result results_links">
 <h2 class="result__title"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.mhp.com%2Fen%2F&amp;rut=abc">MHP &#8211; A <b>Porsche</b> Company</a></h2>
 <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.mhp.com%2Fen%2F&amp;rut=abc">MHP is a technology and business partner, <b>Porsche</b> subsidiary, Ludwigsburg.</a>
</div>
<div class="result"><h2><a class="result__a" href="https://de.wikipedia.org/wiki/MHP">MHP – Wikipedia</a></h2>
 <a class="result__snippet" href="https://de.wikipedia.org/wiki/MHP">Die MHP Management- und IT-Beratung GmbH …</a></div>
<div class="result"><h2><a class="result__a" href="https://de.wikipedia.org/wiki/MHP">duplicate</a></h2></div>
</body></html>"""


def test_ddg_parsing_unwraps_links_and_pairs_snippets():
    results = parse_ddg_html(DDG_PAGE)
    assert [r["url"] for r in results] == ["https://www.mhp.com/en/", "https://de.wikipedia.org/wiki/MHP"]
    assert results[0]["title"] == "MHP – A Porsche Company"
    assert results[0]["snippet"].startswith("MHP is a technology and business partner, Porsche subsidiary")
    assert ddg_target("https://example.org/x") == "https://example.org/x"
    assert parse_ddg_html("<html>no results</html>") == []


def test_html_to_text_drops_chrome_and_keeps_title():
    page = ("<html><head><title>Transponder 3064</title><style>p{}</style></head><body><nav>Home Shop</nav>"
            "<h1>SimonsVoss Transponder 3064</h1><script>track()</script><p>Normalerweise 37 € bis 40 €.</p>"
            "<div>Lieferung: <b>3 Tage</b></div><footer>Impressum</footer></body></html>")
    title, text = html_to_text(page)
    assert title == "Transponder 3064"
    assert text == "SimonsVoss Transponder 3064\nNormalerweise 37 € bis 40 €.\nLieferung: 3 Tage"
    assert html_to_text("<p>" + "x" * 50 + "</p>", max_chars=10)[1] == "x" * 10 + "…"


def test_only_public_http_hosts_can_be_opened():
    assert public_http_url(" https://www.mhp.com/en/ ") == "https://www.mhp.com/en/"
    for bad in ("ftp://x.org/a", "file:///etc/passwd", "http://localhost:8080/", "http://127.0.0.1/", "http://10.0.0.5/",
                "http://[::1]/", "http://db.internal/", "not a url"):
        with pytest.raises(ValueError):
            public_http_url(bad)


async def test_search_and_open_over_http():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.headers.get("user-agent", "")))
        if request.url.host == "html.duckduckgo.com":
            assert b"q=MHP+Porsche" in request.content
            return httpx.Response(200, text=DDG_PAGE)
        if request.url.host == "www.mhp.com":
            return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                                  text="<html><head><title>MHP</title></head><body><p>MHP is a Porsche company.</p></body></html>")
        if request.url.host == "api.tavily.com":
            return httpx.Response(200, json={"results": [{"title": "T", "url": "https://t.example/", "content": "c" * 500}]})
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ddg = DuckDuckGo(http)
    results = await ddg.search("MHP Porsche")
    assert results[0]["url"] == "https://www.mhp.com/en/" and "Peyk" in seen[0][2]
    page = await ddg.open("https://www.mhp.com/en/")
    assert page == {"url": "https://www.mhp.com/en/", "title": "MHP", "text": "MHP is a Porsche company."}
    with pytest.raises(ValueError, match="not a readable page"):
        await ddg.open("https://files.example/a.pdf")
    tavily = Tavily("tvly-x", http)
    got = await tavily.search("anything")
    assert got == [{"title": "T", "url": "https://t.example/", "snippet": "c" * 400}]


def test_engine_factory():
    assert make_web_searcher("off") is None
    assert isinstance(make_web_searcher("duckduckgo"), DuckDuckGo)
    assert isinstance(make_web_searcher("tavily", tavily_api_key="k"), Tavily)
    with pytest.raises(RuntimeError):
        make_web_searcher("tavily")


@pytest.mark.llm
async def test_real_duckduckgo():
    results = await DuckDuckGo().search("MHP Porsche company Ludwigsburg")
    assert results and any("mhp" in r["url"].lower() for r in results), results
