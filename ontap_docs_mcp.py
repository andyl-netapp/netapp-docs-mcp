#!/usr/bin/env python3
"""
ontap-docs-mcp  —  NetApp ONTAP Documentation MCP Server

Provides BM25 keyword search and page retrieval for NetApp ONTAP documentation
sourced from the NetAppDocs/ontap GitHub repository.  All content is cached
locally in a SQLite FTS5 database at ~/.ontap-docs-mcp/index.db.

Tools
-----
  build_search_index   Crawl & cache docs pages from GitHub  (run once)
  search_ontap_docs    BM25 full-text search across indexed pages
  get_ontap_doc        Fetch the full text of a specific page
  list_ontap_sections  Browse top-level sections or topics within a section
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import httpx

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    sys.exit("ERROR: 'mcp' package not found.  Install with:  pip install mcp")

# ── Configuration ─────────────────────────────────────────────────────────────

APP_NAME     = "ontap-docs"
DB_DIR       = Path.home() / ".ontap-docs-mcp"
DB_PATH      = DB_DIR / "index.db"
GITHUB_RAW   = "https://raw.githubusercontent.com/NetAppDocs/ontap/main"
GITHUB_TREES = (
    "https://api.github.com/repos/NetAppDocs/ontap/git/trees/main?recursive=1"
)
HTTP_HEADERS = {"User-Agent": "ontap-docs-mcp/1.0"}

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("ontap-docs-mcp")

# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    APP_NAME,
    instructions=(
        "NetApp ONTAP documentation search and retrieval. "
        "Call build_search_index() once to populate the local cache, "
        "then use search_ontap_docs() to find pages and get_ontap_doc() "
        "to read full content."
    ),
)

# ── Database ──────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    """Open (and initialise) the SQLite database."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS docs (
            path       TEXT PRIMARY KEY,
            title      TEXT NOT NULL DEFAULT '',
            section    TEXT NOT NULL DEFAULT '',
            content    TEXT NOT NULL DEFAULT '',
            indexed_at TEXT DEFAULT (datetime('now'))
        );

        -- FTS5 table: path/section are UNINDEXED (stored but not tokenised)
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            path      UNINDEXED,
            title,
            section   UNINDEXED,
            content,
            tokenize  = 'unicode61 remove_diacritics 2'
        );

        -- file_tree: every .adoc path discovered in the GitHub repo tree
        CREATE TABLE IF NOT EXISTS file_tree (
            path    TEXT PRIMARY KEY,
            section TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.commit()
    return conn


def _upsert_doc(
    conn: sqlite3.Connection,
    path: str,
    title: str,
    section: str,
    content: str,
) -> None:
    """Insert or replace a document in both the docs and FTS tables."""
    conn.execute(
        "INSERT OR REPLACE INTO docs (path, title, section, content) VALUES (?,?,?,?)",
        (path, title, section, content),
    )
    # FTS5 does not support ON CONFLICT — delete first, then insert
    conn.execute("DELETE FROM docs_fts WHERE path = ?", (path,))
    conn.execute(
        "INSERT INTO docs_fts (path, title, section, content) VALUES (?,?,?,?)",
        (path, title, section, content),
    )
    conn.commit()


# ── AsciiDoc → plain text ─────────────────────────────────────────────────────

_STRIP_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^////.*?////\s*",           re.S | re.M), ""),   # block comments
    (re.compile(r"^//.*$",                    re.M),         ""),   # line comments
    (re.compile(r"^:[^:]+:.*$",              re.M),         ""),   # attribute defs
    (re.compile(r"^include::[^\[]+\[.*?\].*$", re.M),       ""),   # includes
    (re.compile(r"^\[.*?\]\s*$",             re.M),         ""),   # block roles
    (re.compile(r"^={4,}$|^-{4,}$|^\*{4,}$|^\+{4,}$|^_{4,}$", re.M), ""),  # delimiters
    (re.compile(r"ifdef::.*?\n(.*?)endif::.*?\n", re.S | re.M), r"\1"),
    (re.compile(r"link:([^\[]+)\[([^\]]*)\]"), r"\2"),      # link macro → text
    (re.compile(r"xref:[^\[]+\[([^\]]*)\]"),   r"\1"),      # xref → text
    (re.compile(r"image::[^\[]+\[.*?\]"),       ""),         # images
    (re.compile(r"\*{1,2}([^*\n]+)\*{1,2}"),   r"\1"),      # bold
    (re.compile(r"_{1,2}([^_\n]+)_{1,2}"),     r"\1"),      # italic
    (re.compile(r"`{1,2}([^`\n]+)`{1,2}"),     r"\1"),      # monospace
    (re.compile(r"\n{3,}"),                     "\n\n"),     # excess blank lines
]


def _adoc_to_text(src: str) -> tuple[str, str]:
    """Return (title, plain_text) stripped of AsciiDoc markup."""
    m = re.search(r"^=\s+(.+)$", src, re.M)
    title = m.group(1).strip() if m else ""
    text = src
    for pat, repl in _STRIP_RULES:
        text = pat.sub(repl, text)
    # Convert remaining heading markers (== … ======) to plain text
    text = re.sub(r"^={1,6}\s+", "", text, flags=re.M)
    return title, text.strip()


# ── GitHub file tree ─────────────────────────────────────────────────────────

async def _fetch_file_tree(client: httpx.AsyncClient) -> list[str]:
    """Return all .adoc paths in the repo (excludes _include/ fragments)."""
    r = await client.get(
        GITHUB_TREES,
        headers=HTTP_HEADERS,
        timeout=30,
    )
    if r.status_code != 200:
        log.error("GitHub Trees API: HTTP %d", r.status_code)
        return []
    data = r.json()
    if data.get("truncated"):
        log.warning("GitHub tree response was truncated — some files may be missing")
    return [
        item["path"]
        for item in data.get("tree", [])
        if item["path"].endswith(".adoc")
        and not item["path"].startswith("_include/")
        and not item["path"].startswith("_")
    ]


# ── HTTP helper ───────────────────────────────────────────────────────────────

async def _fetch(url: str, client: httpx.AsyncClient) -> Optional[str]:
    try:
        r = await client.get(
            url, headers=HTTP_HEADERS, follow_redirects=True, timeout=20
        )
        return r.text if r.status_code == 200 else None
    except Exception as exc:
        log.warning("fetch %s  →  %s", url, exc)
        return None


# ── Path normalisation ────────────────────────────────────────────────────────

def _normalise_path(path: str) -> str:
    """Convert any supported path/URL format to a relative .adoc path."""
    if "docs.netapp.com" in path:
        m = re.search(r"/us-en/ontap/(.+?)(?:\.html)?(?:[?#].*)?$", path)
        if m:
            return m.group(1) + ".adoc"
    if "raw.githubusercontent.com" in path:
        m = re.search(r"/NetAppDocs/ontap/(?:main|[^/]+)/(.+)", path)
        if m:
            return m.group(1)
    return path.lstrip("/").replace(".html", ".adoc")


def _prep_fts5_query(query: str) -> str:
    """Prepare a user query for FTS5 MATCH.

    Wraps hyphenated tokens in double quotes so FTS5 doesn't misinterpret
    the '-' as the NOT operator.  Structured queries (AND/OR/NOT/quotes)
    are passed through unchanged.
    """
    if re.search(r'\b(?:AND|OR|NOT)\b|"', query):
        return query   # already structured — pass through
    tokens = query.split()
    result = []
    for tok in tokens:
        if re.search(r"[-:*^()]", tok):
            result.append(f'"{tok}"')
        else:
            result.append(tok)
    return " ".join(result)


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def build_search_index(section_filter: str = "", max_pages: int = 300) -> str:
    """Build or refresh the local ONTAP documentation search index from GitHub.

    Downloads AsciiDoc pages from the NetAppDocs/ontap GitHub repo and stores
    them in a SQLite FTS5 index for fast BM25 keyword search.

    Call this once after installing the server.  Re-run to pick up new pages
    (already-cached pages are skipped).

    Args:
        section_filter: Only index this section, e.g. 'ndmp', 'snapmirror',
                        'networking', 'san-admin'.  Leave empty for all sections.
        max_pages:      Maximum pages to fetch per run (default 300).
                        Call again to continue where it left off.
    """
    conn = _get_db()
    async with httpx.AsyncClient() as client:
        log.info("Fetching file tree from GitHub …")
        all_paths = await _fetch_file_tree(client)
        if not all_paths:
            conn.close()
            return "❌ Could not fetch file tree — check network connectivity."

        log.info("File tree: %d .adoc files", len(all_paths))

        # Persist the file tree
        conn.execute("DELETE FROM file_tree")
        for path in all_paths:
            section = path.split("/")[0] if "/" in path else ""
            conn.execute(
                "INSERT OR REPLACE INTO file_tree (path, section) VALUES (?,?)",
                (path, section),
            )
        conn.commit()

        if section_filter:
            all_paths = [p for p in all_paths if p.split("/")[0] == section_filter]
            log.info("Filtered to section '%s': %d files", section_filter, len(all_paths))

        already = {r[0] for r in conn.execute("SELECT path FROM docs")}
        to_fetch = [p for p in all_paths if p not in already][:max_pages]
        log.info("Fetching %d new pages …", len(to_fetch))

        indexed = skipped = 0
        for path in to_fetch:
            raw = await _fetch(f"{GITHUB_RAW}/{path}", client)
            if not raw:
                skipped += 1
                continue
            title, text = _adoc_to_text(raw)
            if not title:
                title = (
                    path.split("/")[-1]
                    .replace("-", " ")
                    .replace(".adoc", "")
                    .title()
                )
            section = path.split("/")[0] if "/" in path else ""
            _upsert_doc(conn, path, title, section, text)
            indexed += 1
            if indexed % 50 == 0:
                log.info("  … %d / %d indexed", indexed, len(to_fetch))
            await asyncio.sleep(0.05)   # be polite to GitHub

    total_cached = indexed + len(already)
    remaining = max(0, len(all_paths) - len(already) - indexed)
    suffix = (
        f"  Remaining: {remaining} pages — call build_search_index() again to continue."
        if len(to_fetch) == max_pages and remaining > 0
        else "  Index complete for selected sections!"
    )
    conn.close()
    return (
        f"✅  {indexed} pages indexed, {skipped} skipped.\n"
        f"Total cached: {total_cached} pages.{suffix}"
    )


@mcp.tool()
async def search_ontap_docs(query: str, section: str = "", limit: int = 10) -> str:
    """Search ONTAP documentation by keyword using BM25 ranking.

    Args:
        query:   Search terms, e.g. 'configure NDMP node scope'
                 or 'snapmirror break resync' or 'NVMe namespace mapping'
        section: Restrict to a top-level section, e.g. 'ndmp', 'snapmirror',
                 'networking', 'san-admin', 'nas-audit'.  Leave empty for all.
        limit:   Maximum results to return (default 10, max 25).
    """
    conn = _get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
        if total == 0:
            return (
                "⚠️  No documents indexed yet.\n"
                "Run `build_search_index()` first to populate the local cache."
            )

        limit = min(limit, 25)
        fts_query = _prep_fts5_query(query)

        def _run_search(q: str, sec: str) -> list:
            if sec:
                return conn.execute(
                    """
                    SELECT path, title, section,
                           snippet(docs_fts, 3, '>>>', '<<<', ' … ', 32) AS snip
                    FROM   docs_fts
                    WHERE  docs_fts MATCH ?
                    AND    section = ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (q, sec, limit),
                ).fetchall()
            return conn.execute(
                """
                SELECT path, title, section,
                       snippet(docs_fts, 3, '>>>', '<<<', ' … ', 32) AS snip
                FROM   docs_fts
                WHERE  docs_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (q, limit),
            ).fetchall()

        # Try AND search first; fall back to OR if no hits
        rows = _run_search(fts_query, section)
        used_or = False
        if not rows and len(fts_query.split()) > 1:
            or_query = " OR ".join(
                t for t in fts_query.split() if t not in ("AND", "OR", "NOT")
            )
            rows = _run_search(or_query, section)
            used_or = bool(rows)

        if not rows:
            hint = f" in section '{section}'" if section else ""
            return (
                f"No results for `{query}`{hint}.\n"
                "Try fewer or different keywords, broaden the section filter, "
                "or run `build_search_index()` to index more pages."
            )

        qualifier = "  _(OR match — not all terms found together)_" if used_or else ""
        out = [
            f"## Results for `{query}`"
            + (f"  _(section: {section})_" if section else "")
            + qualifier,
            "",
        ]
        for i, r in enumerate(rows, 1):
            out.append(f"**{i}. {r['title'] or r['path']}**")
            out.append(f"   Path: `{r['path']}`  |  Section: `{r['section']}`")
            out.append(f"   {r['snip']}")
            out.append("")
        out.append(
            f"_{total} pages in index.  Use `get_ontap_doc(path)` to read a full page._"
        )
        return "\n".join(out)

    except Exception as exc:
        return (
            f"❌ Search error: {exc}\n\n"
            "Tip: quote exact phrases with double quotes, e.g. `\"node-scoped NDMP\"`"
        )
    finally:
        conn.close()


@mcp.tool()
async def get_ontap_doc(path: str) -> str:
    """Fetch the full text of a specific ONTAP documentation page.

    Returns the cached version if available; otherwise fetches live from GitHub
    and caches the result for future calls.

    Args:
        path: One of:
              • Relative path:         'ndmp/configure-node-scoped-ndmp-concept.adoc'
              • With .html extension:  'ndmp/configure-node-scoped-ndmp-concept.html'
              • docs.netapp.com URL:   'https://docs.netapp.com/us-en/ontap/ndmp/...'
              • GitHub raw URL:        'https://raw.githubusercontent.com/NetAppDocs/...'
    """
    path = _normalise_path(path)

    # Serve from cache
    conn = _get_db()
    row = conn.execute(
        "SELECT title, content FROM docs WHERE path = ?", (path,)
    ).fetchone()
    conn.close()

    if row:
        return (
            f"# {row['title']}\n\n"
            f"> _Cached — source: `{GITHUB_RAW}/{path}`_\n\n"
            f"{row['content']}"
        )

    # Fetch live
    url = f"{GITHUB_RAW}/{path}"
    async with httpx.AsyncClient() as client:
        raw = await _fetch(url, client)

    if not raw:
        return (
            f"❌ Page not found: `{path}`\n\n"
            "Check the path with `list_ontap_sections()` or `search_ontap_docs()`."
        )

    title, text = _adoc_to_text(raw)
    if not title:
        title = (
            path.split("/")[-1]
            .replace("-", " ")
            .replace(".adoc", "")
            .title()
        )
    section = path.split("/")[0] if "/" in path else ""

    conn = _get_db()
    _upsert_doc(conn, path, title, section, text)
    conn.close()

    return (
        f"# {title}\n\n"
        f"> _Fetched from `{GITHUB_RAW}/{path}`_\n\n"
        f"{text}"
    )


@mcp.tool()
async def list_ontap_sections(section: str = "") -> str:
    """List ONTAP documentation sections or topics within a section.

    Args:
        section: Top-level section name, e.g. 'ndmp', 'snapmirror', 'networking'.
                 Leave empty to list all top-level sections.
    """
    conn = _get_db()
    try:
        if section:
            # Files within a specific section
            rows = conn.execute(
                "SELECT ft.path, d.title FROM file_tree ft "
                "LEFT JOIN docs d ON d.path = ft.path "
                "WHERE ft.section = ? ORDER BY ft.path",
                (section,),
            ).fetchall()
            if not rows:
                sections = [
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT section FROM file_tree "
                        "WHERE section != '' ORDER BY section"
                    ).fetchall()
                ]
                return (
                    f"Section '{section}' not found.\n"
                    "Available sections: "
                    + (", ".join(sections) if sections else "none — run build_search_index() first")
                )
            indexed_count = sum(1 for r in rows if r["title"])
            out = [f"## Files in `{section}`  ({len(rows)} total, {indexed_count} indexed)\n"]
            for r in rows:
                label = r["title"] or r["path"].split("/")[-1].replace("-", " ").replace(".adoc", "")
                cached = "" if r["title"] else "  _(not indexed)_"
                out.append(f"- {label}{cached}  →  `{r['path']}`")
            return "\n".join(out)

        else:
            # All top-level sections — prefer file_tree, fall back to docs
            rows = conn.execute(
                "SELECT section, COUNT(*) c FROM file_tree "
                "WHERE section != '' GROUP BY section ORDER BY section"
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT section, COUNT(*) c FROM docs "
                    "WHERE section != '' GROUP BY section ORDER BY section"
                ).fetchall()
            if not rows:
                return (
                    "No sections found.  "
                    "Run `build_search_index()` to populate the cache."
                )
            # Count indexed pages per section
            indexed_map = dict(
                conn.execute(
                    "SELECT section, COUNT(*) FROM docs "
                    "WHERE section != '' GROUP BY section"
                ).fetchall()
            )
            out = [
                f"## ONTAP Documentation Sections  "
                f"({sum(r['c'] for r in rows)} total files)\n"
            ]
            for r in rows:
                idx = indexed_map.get(r["section"], 0)
                out.append(
                    f"- **{r['section']}**  "
                    f"({r['c']} files, {idx} indexed)"
                )
            out.append(
                "\nUse `list_ontap_sections(section='ndmp')` to see files in a section."
            )
            return "\n".join(out)

    finally:
        conn.close()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
