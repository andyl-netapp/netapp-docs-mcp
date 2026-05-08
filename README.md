# netapp-docs-mcp

An on-demand [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that gives AI assistants (GitHub Copilot, Claude, etc.) live access to **all NetApp product documentation** at [docs.netapp.com](https://docs.netapp.com).

No pre-indexing, no database, no API key required — pages are fetched in real time.

---

## Servers

This repo contains two MCP servers:

| File | MCP name | Description |
|------|----------|-------------|
| `netapp_docs_mcp.py` | `netapp-docs` | **Recommended** — on-demand access to all NetApp docs |
| `ontap_docs_mcp.py`  | `ontap-docs`  | Legacy — BM25 local index for ONTAP docs only |

---

## `netapp_docs_mcp.py` — Tools

| Tool | Description |
|------|-------------|
| `list_products(lang)` | List all NetApp product doc sets available on docs.netapp.com |
| `get_product_toc(product, lang)` | Get the full page list (table of contents) for a product |
| `get_page(url)` | Fetch and read any docs.netapp.com page as clean text |
| `search_docs(query, product, lang, max_results)` | Search across all NetApp docs via GitHub code search |

### Coverage

- All products: ONTAP, StorageGRID, Cloud Volumes ONTAP, SANtricity, Active IQ, and 100+ more
- Multiple languages: `us-en` (default), `zh-cn`, `ja-jp`, `de-de`, `ko-kr`, `fr-fr`, etc.
- Versioned references: ONTAP REST API (`ontap-restapi-9141`, `9131`, …) and EMS (`ontap-ems-9141`, …)

---

## Installation

### Prerequisites

```bash
pip install mcp httpx
```

### Register with GitHub Copilot CLI

Add to `~/.copilot/mcp-config.json`:

```json
{
  "mcpServers": {
    "netapp-docs": {
      "command": "python",
      "args": ["/path/to/netapp_docs_mcp.py"],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

### Register with Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "netapp-docs": {
      "command": "python",
      "args": ["/path/to/netapp_docs_mcp.py"]
    }
  }
}
```

---

## Usage Examples

### List all available products
```
list_products()
list_products(lang="zh-cn")      # Chinese docs
```

### Browse a product's documentation
```
get_product_toc("ontap")
get_product_toc("storagegrid-118")
get_product_toc("ontap-restapi-9141")    # ONTAP 9.14.1 REST API
get_product_toc("ontap-ems-9131")        # ONTAP 9.13.1 EMS reference
```

### Read a specific page
```
get_page("https://docs.netapp.com/us-en/ontap/ndmp/enable-node-scoped-ndmp-cluster-task.html")
get_page("https://docs.netapp.com/us-en/storagegrid-118/admin/index.html")
```

### Search across all NetApp docs
```
search_docs("node-scope NDMP")
search_docs("SnapMirror SVM DR failover")
search_docs("NVMe namespace", product="ontap")          # ONTAP only
search_docs("bucket lifecycle", product="storagegrid-118")
```

---

## Architecture

```
AI Assistant (Copilot / Claude)
        │  MCP tools
        ▼
netapp_docs_mcp.py
        │
        ├── list_products()    ──► docs.netapp.com/sitemap.xml  (XML parse)
        ├── get_product_toc()  ──► docs.netapp.com/{product}/sitemap.xml
        ├── get_page()         ──► docs.netapp.com/{url}  (HTML → clean text)
        └── search_docs()      ──► api.github.com/search/code  (org:NetAppDocs)
```

- **Page fetch**: `httpx` downloads the HTML; a custom `HTMLParser` strips site
  chrome and returns clean Markdown-style text.
- **Search**: GitHub code search across the `NetAppDocs` org searches all
  `.adoc` source files.  Results include direct `docs.netapp.com` URLs.
- **No local storage**: nothing is written to disk; every call hits the network.

---

## Notes

- GitHub code search rate limit: ~10 unauthenticated requests/minute.
  Set a `GITHUB_TOKEN` environment variable to raise the limit to 30/min.
- Main ONTAP admin docs (`us-en/ontap/`) reflect the **latest** ONTAP release.
  Older versioned docs are available for REST API (`ontap-restapi-XYZW`) and
  EMS (`ontap-ems-XYZW`) references.

---

## License

MIT
