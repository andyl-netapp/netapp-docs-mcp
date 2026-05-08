#!/usr/bin/env python3
"""
netapp-docs-mcp  —  NetApp Documentation MCP Server (on-demand)

Provides on-demand access to ALL NetApp product documentation at
https://docs.netapp.com — no pre-indexing required, always up-to-date.

Tools
-----
  list_products       List all available NetApp product doc sets
  search_docs         Search across all NetApp docs (GitHub code search)
  get_page            Fetch and read any docs.netapp.com page
  get_product_toc     List all pages available for a product
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Optional

import httpx

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.exit("ERROR: 'mcp' package not found. Install with:  pip install mcp")

# ── Configuration ─────────────────────────────────────────────────────────────

APP_NAME   = "netapp-docs"
DOCS_BASE  = "https://docs.netapp.com"
GH_API     = "https://api.github.com"
GH_ORG     = "NetAppDocs"
USER_AGENT = "netapp-docs-mcp/1.0 (research tool)"

mcp = FastMCP(APP_NAME)

# ── HTML → text converter ─────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    """
    Converts docs.netapp.com HTML to clean plain text.

    Skips nav/header/footer/script/style elements; renders headings with
    Markdown-style # prefix; preserves code blocks with backtick fences.
    """

    _SKIP_TAGS  = {"script", "style", "noscript"}
    _BLOCK_TAGS = {"p", "div", "section", "article", "aside",
                   "ul", "ol", "li", "tr", "td", "th",
                   "blockquote", "figure", "figcaption"}
    _CODE_TAGS  = {"pre", "code"}
    _HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    # CSS classes/ids that belong to site chrome (skip entirely)
    _SKIP_CLASSES = {
        "sidebar", "toc", "breadcrumb", "pagination",
        "navbar", "header", "footer", "nav", "menu",
        "side-nav", "site-nav", "toolbar", "banner",
        "skip-link", "feedback",
    }

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0
        self._code_depth = 0
        self._in_pre     = False

    # ── internal helpers ──────────────────────────────────────────────────────

    def _should_skip(self, attrs: list) -> bool:
        d = dict(attrs)
        cls  = d.get("class", "")
        role = d.get("role", "")
        eid  = d.get("id", "")
        combined = f"{cls} {role} {eid}".lower()
        return any(skip in combined for skip in self._SKIP_CLASSES)

    # ── HTMLParser overrides ──────────────────────────────────────────────────

    def handle_starttag(self, tag: str, attrs: list):
        tag = tag.lower()

        if tag in self._SKIP_TAGS or self._should_skip(attrs):
            self._skip_depth += 1
            return

        if self._skip_depth > 0:
            return

        if tag == "pre":
            self._in_pre = True
            self.parts.append("\n```\n")
        elif tag in self._CODE_TAGS and not self._in_pre:
            self._code_depth += 1
            if self._code_depth == 1:
                self.parts.append("`")
        elif tag in self._HEADING_TAGS:
            level = int(tag[1])
            self.parts.append("\n" + "#" * level + " ")
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "a":
            pass  # ignore links, just keep text

    def handle_endtag(self, tag: str):
        tag = tag.lower()

        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return

        if self._skip_depth > 0:
            return

        if tag == "pre":
            self._in_pre = False
            self.parts.append("\n```\n")
        elif tag in self._CODE_TAGS and not self._in_pre and self._code_depth > 0:
            self._code_depth -= 1
            if self._code_depth == 0:
                self.parts.append("`")
        elif tag in self._BLOCK_TAGS | self._HEADING_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if self._skip_depth > 0:
            return
        if self._in_pre:
            self.parts.append(data)
        else:
            text = data.strip()
            if text:
                self.parts.append(text + " ")

    def get_text(self) -> str:
        text = "".join(self.parts)
        # Collapse 3+ blank lines → 2
        text = re.sub(r"\n{3,}", "\n\n", text)
        # Collapse multiple spaces (but not in code blocks)
        lines = []
        in_code = False
        for line in text.splitlines():
            if line.strip().startswith("```"):
                in_code = not in_code
                lines.append(line)
            elif not in_code:
                lines.append(re.sub(r" {2,}", " ", line))
            else:
                lines.append(line)
        return "\n".join(lines).strip()


def _html_to_text(html: str) -> str:
    """Parse HTML and return clean text, looking for main content area first."""
    # Try to extract main content area to avoid nav/chrome
    # docs.netapp.com wraps main content in <div class="sect1"> or <main> or <article>
    main_match = re.search(
        r'<(?:main|article)[^>]*>(.*?)</(?:main|article)>',
        html, re.DOTALL | re.IGNORECASE
    )
    if main_match:
        html_fragment = main_match.group(1)
    else:
        # Try <div id="content"> or <div class="content">
        content_match = re.search(
            r'<div[^>]+(?:id|class)=["\']content["\'][^>]*>(.*?)</div>',
            html, re.DOTALL | re.IGNORECASE
        )
        html_fragment = content_match.group(1) if content_match else html

    extractor = _TextExtractor()
    extractor.feed(html_fragment)
    return extractor.get_text()


# ── HTTP helpers ───────────────────────────────────────────────────────────────

async def _fetch(url: str, accept: str = "text/html,application/xhtml+xml") -> str:
    """GET a URL and return the response body as text."""
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return r.text


async def _gh_search(q: str, per_page: int = 20) -> dict:
    """Run a GitHub code search query and return the parsed JSON response."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    params = {"q": q, "per_page": per_page}
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        r = await client.get(f"{GH_API}/search/code", headers=headers, params=params)
        return r.json()


def _gh_to_doc_url(repo_name: str, file_path: str, lang: str = "us-en") -> str:
    """
    Convert a NetAppDocs GitHub repo + file path to a docs.netapp.com URL.

    Example:
      repo_name = "ontap"
      file_path = "ndmp/enable-node-scoped-ndmp-cluster-task.adoc"
      → https://docs.netapp.com/us-en/ontap/ndmp/enable-node-scoped-ndmp-cluster-task.html
    """
    if "_include/" in file_path or file_path.startswith("_"):
        return ""  # fragment include — no standalone page
    doc_path = re.sub(r"\.adoc$", ".html", file_path)
    return f"{DOCS_BASE}/{lang}/{repo_name}/{doc_path}"


def _friendly_title(file_path: str) -> str:
    """Turn a file basename into a human-readable title."""
    name = file_path.rsplit("/", 1)[-1]
    name = re.sub(r"\.adoc$", "", name)
    name = re.sub(r"[-_]", " ", name)
    return name.title()


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_products(lang: str = "us-en") -> str:
    """
    List all available NetApp product documentation sets on docs.netapp.com.

    Parses the site's sitemap index to discover every product that has
    documentation in the given language.

    Args:
        lang: Language/locale code (default: 'us-en').  Other values:
              'zh-cn', 'ja-jp', 'de-de', 'fr-fr', 'ko-kr', etc.
    """
    try:
        xml_text = await _fetch(f"{DOCS_BASE}/sitemap.xml",
                                accept="application/xml,text/xml")
        root = ET.fromstring(xml_text)
        ns   = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        pattern = re.compile(
            rf"https://docs\.netapp\.com/{re.escape(lang)}/([^/]+)/sitemap\.xml"
        )
        products: list[str] = []
        for sitemap in root.findall("sm:sitemap", ns):
            loc = sitemap.findtext("sm:loc", namespaces=ns) or ""
            m = pattern.match(loc)
            if m:
                products.append(m.group(1))

        if not products:
            return f"No products found for language '{lang}'."

        products.sort()
        lines = [
            f"Found {len(products)} product doc sets for '{lang}':",
            "",
            f"{'Product slug':<55}  Docs URL",
            "-" * 85,
        ]
        for p in products:
            lines.append(f"  {p:<53}  {DOCS_BASE}/{lang}/{p}/")
        lines.append("")
        lines.append("Pass any product slug to get_product_toc() or search_docs().")
        return "\n".join(lines)

    except ET.ParseError as e:
        return f"Error parsing sitemap index: {e}"
    except Exception as e:
        return f"Error listing products: {e}"


@mcp.tool()
async def get_product_toc(product: str, lang: str = "us-en") -> str:
    """
    Get the complete table of contents for a NetApp product doc set.

    Returns the list of all pages (relative paths) available for the product,
    parsed from its sitemap.xml.  Use the base URL shown to construct full page
    URLs to pass to get_page().

    Args:
        product: Product slug (e.g. 'ontap', 'storagegrid-118',
                 'cloud-volumes-ontap-relnotes', 'active-iq-unified-manager').
                 Use list_products() to see all available slugs.
        lang:    Language code (default: 'us-en').
    """
    sitemap_url = f"{DOCS_BASE}/{lang}/{product}/sitemap.xml"
    try:
        xml_text = await _fetch(sitemap_url, accept="application/xml,text/xml")
        root     = ET.fromstring(xml_text)
        ns       = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        base     = f"{DOCS_BASE}/{lang}/{product}/"

        pages: list[str] = []
        for url_elem in root.findall("sm:url", ns):
            loc = url_elem.findtext("sm:loc", namespaces=ns) or ""
            if loc.startswith(base):
                pages.append(loc[len(base):])   # relative path only

        if not pages:
            return f"No pages found for product '{product}' (lang='{lang}')."

        pages.sort()
        lines = [
            f"Product: {product}  ({len(pages)} pages)",
            f"Base URL: {base}",
            "",
        ]
        lines.extend(f"  {p}" for p in pages)
        lines.append("")
        lines.append(f"Example: get_page('{base}{pages[0]}')")
        return "\n".join(lines)

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return (
                f"Product '{product}' not found for lang='{lang}'.\n"
                "Use list_products() to see available product slugs."
            )
        return f"HTTP {e.response.status_code} fetching sitemap for '{product}'."
    except ET.ParseError as e:
        return f"Error parsing sitemap for '{product}': {e}"
    except Exception as e:
        return f"Error getting TOC for '{product}': {e}"


@mcp.tool()
async def get_page(url: str) -> str:
    """
    Fetch and read a specific docs.netapp.com page.

    Downloads the page, strips site chrome (nav, header, footer), and returns
    clean, readable text with Markdown-style heading markers.

    Args:
        url: Full URL of the docs page.
             Example: https://docs.netapp.com/us-en/ontap/ndmp/enable-node-scoped-ndmp-cluster-task.html
    """
    if not url.startswith(DOCS_BASE):
        return f"Error: only {DOCS_BASE} URLs are supported."

    try:
        html = await _fetch(url)
        text = _html_to_text(html)
        if not text:
            return f"No readable content found at:\n{url}"
        return f"Source: {url}\n\n{text}"

    except httpx.HTTPStatusError as e:
        return f"HTTP {e.response.status_code}: {url}"
    except Exception as e:
        return f"Error fetching page: {e}"


@mcp.tool()
async def search_docs(
    query: str,
    product: str = "",
    lang: str = "us-en",
    max_results: int = 10,
) -> str:
    """
    Search NetApp documentation using GitHub code search.

    Searches across ALL NetApp product docs (the entire NetAppDocs GitHub org)
    or within a specific product repository.  Returns matching page URLs that
    you can pass to get_page() to read the full content.

    GitHub code search rate limit: ~10 requests/min unauthenticated.

    Args:
        query:       Search keywords, e.g. 'node-scope NDMP', 'SnapMirror SVM DR'
        product:     Optional product slug to limit search scope
                     (e.g. 'ontap', 'storagegrid-118').  Leave blank to search all products.
        lang:        Language for result URLs (default: 'us-en').
        max_results: Max results to return (1–30, default: 10).
    """
    max_results = max(1, min(int(max_results), 30))

    # Build the GitHub search query
    scope = f"repo:{GH_ORG}/{product}" if product else f"org:{GH_ORG}"
    gh_q  = f"{query} {scope} extension:adoc"

    try:
        data = await _gh_search(gh_q, per_page=max_results)

        if "message" in data and "items" not in data:
            return f"GitHub API error: {data['message']}"

        items = data.get("items", [])
        total = data.get("total_count", 0)

        if not items:
            suffix = f" in '{product}'" if product else " across all NetApp docs"
            return f"No results found for '{query}'{suffix}."

        lines = [
            f"Search: '{query}'" + (f"  [product: {product}]" if product else "  [all products]"),
            f"Total matches: {total}  (showing {len(items)})",
            "",
        ]

        shown = 0
        for item in items:
            repo_name = item["repository"]["name"]
            file_path = item["path"]
            doc_url   = _gh_to_doc_url(repo_name, file_path, lang)
            if not doc_url:
                continue
            title  = _friendly_title(file_path)
            gh_url = item["html_url"]
            lines += [
                f"### {title}",
                f"  Product : {repo_name}",
                f"  Docs URL: {doc_url}",
                f"  GitHub  : {gh_url}",
                "",
            ]
            shown += 1

        if shown == 0:
            return (f"No public docs pages matched '{query}'"
                    + (f" in '{product}'" if product else "") + ".")

        lines.append(f"Use get_page(<Docs URL>) to read the full content of any result.")
        return "\n".join(lines)

    except Exception as e:
        return f"Error searching docs: {e}"


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
