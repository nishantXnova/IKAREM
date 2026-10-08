"""Site SEO consistency: sitemap <-> vercel routes <-> files <-> heads.

Every indexed URL must resolve (route + file), and every page must carry
the head tags search engines need (title, description, canonical, og).
Catches dead links and head regressions before deploy, not after.
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

SITE = Path("site")
BASE = "https://ikarem.vercel.app"


def _sitemap_paths():
    tree = ET.parse(SITE / "sitemap.xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return [u.find("s:loc", ns).text for u in tree.getroot().findall("s:url", ns)]


def _vercel_routes():
    return json.loads(Path("vercel.json").read_text(encoding="utf-8"))["routes"]


def test_sitemap_urls_have_routes_and_files():
    routes = _vercel_routes()
    dests = {r["src"]: r["dest"] for r in routes}
    for loc in _sitemap_paths():
        assert loc.startswith(BASE), loc
        path = loc[len(BASE) :] or "/"
        if path.endswith((".pdf", ".txt", ".xml", ".png")):
            dest = dests.get(path)
            assert dest, f"sitemap {loc} has no vercel route"
            assert (Path(dest.lstrip("/"))).exists(), f"sitemap {loc} -> missing {dest}"
        else:
            dest = dests.get(path) or dests.get(path + ".html")
            assert dest, f"sitemap {loc} has no vercel route"
            assert (Path(dest.lstrip("/"))).exists(), f"sitemap {loc} -> missing {dest}"


def test_extension_pages_indexed_and_routed():
    locs = _sitemap_paths()
    for url in (f"{BASE}/extensions", f"{BASE}/extensions/pentest", f"{BASE}/extensions/oauth"):
        assert url in locs, f"{url} missing from sitemap"
    routes = {r["src"]: r["dest"] for r in _vercel_routes()}
    assert routes["/extensions"] == "/site/extensions.html"
    assert routes["/extensions/pentest"] == "/site/ext-pentest.html"
    assert routes["/extensions/oauth"] == "/site/ext-oauth.html"


def _pages():
    return sorted(p for p in SITE.glob("*.html") if not p.name.startswith("google"))


def test_every_page_has_seo_head():
    pages = _pages()
    assert len(pages) >= 8, "site lost pages?"
    for p in pages:
        head = p.read_text(encoding="utf-8").split("</head>")[0]
        assert re.search(r"<title>[^<]{10,}", head), f"{p.name}: weak title"
        assert 'name="description"' in head, f"{p.name}: no meta description"
        assert 'rel="canonical"' in head, f"{p.name}: no canonical"
        assert 'property="og:url"' in head, f"{p.name}: no og:url"


def test_internal_links_resolve():
    pages = {p.name for p in SITE.glob("*.html")}
    for p in SITE.glob("*.html"):
        for href in re.findall(r'href="([^"#]+?)"', p.read_text(encoding="utf-8")):
            if href.startswith(("http", "mailto:")) or "/" in href and not href.endswith(".html"):
                continue
            if href.endswith(".html"):
                assert href in pages, f"{p.name} links dead {href}"


def test_nav_lists_extensions_everywhere():
    for p in _pages():
        assert 'href="extensions.html"' in p.read_text(encoding="utf-8"), f"{p.name}: nav missing Extensions"


def test_json_ld_parses():
    import json

    for p in _pages():
        blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', p.read_text(), re.S)
        assert blocks, f"{p.name}: no JSON-LD"
        for b in blocks:
            json.loads(b)


def test_llms_covers_extensions():
    text = (SITE / "llms.txt").read_text(encoding="utf-8")
    assert "ikarem-pentest" in text and "ikarem-oauth" in text
