#!/usr/bin/env python3
"""Minimal MCP (stdio) web-search server for local models.

- Pure Python standard library (no pip installs, no API keys, no cost).
- Search: Bing (primary), then Baidu, then 360 (So.com) as fallbacks.
- Each engine uses a markup-specific parser and falls back to a generic
  "heading + external link" parser, so a site redesign degrades instead
  of breaking.
- Also provides fetch_page() to read a specific URL as text.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout (MCP stdio).
Run with --selftest to check search/fetch without speaking MCP.
"""

import sys
import os
import io
import json
import re
import html
import time
import base64
import datetime
import urllib.parse
import urllib.request
import urllib.error

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web-search-mcp.log")


def log_event(message):
    """Append a line to the server log so tool usage can be verified."""
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (stamp, message))
    except Exception:
        pass


# --- UTF-8 stdio (Windows console defaults to cp936 and would corrupt JSON) ---
try:
    sys.stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", newline="\n")
except Exception:
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}

# Matches a tag while honouring quoted attribute values (which may contain ">").
TAG_RE = re.compile(r"""<[^>"']*(?:"[^"]*"[^>"']*|'[^']*'[^>"']*)*>""", re.S)

# Avoid hammering the engines: cache successful queries, pace repeat calls.
_CACHE = {}
_LAST_CALL = {}
_MIN_ENGINE_INTERVAL = 1.0


def http_get(url, timeout=25, max_bytes=4_000_000):
    """GET a URL and decode it into text, retrying once on a transient failure."""
    last_error = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout if attempt == 0 else 15) as resp:
                raw = resp.read(max_bytes)
            break
        except Exception as exc:  # timeout / DNS / reset — networks are flaky
            last_error = exc
            if attempt == 0:
                time.sleep(1.0)
    else:
        raise last_error
    head = raw[:4096].decode("latin-1", "ignore").lower()
    m = re.search(r'charset=["\']?([\w-]+)', head)
    encodings = []
    if m:
        encodings.append(m.group(1))
    encodings += ["utf-8", "gb18030", "latin-1"]
    for enc in encodings:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def strip_tags(fragment):
    """Remove markup and collapse whitespace.

    TAG_RE respects quoted attribute values, so markup like <a data-x="a>b">
    does not leak raw attributes into the text.
    """
    if not fragment:
        return ""
    s = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", fragment)
    s = TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def decode_bing_link(href):
    """Bing wraps results in /ck/a?...&u=a1<base64url>; unwrap it."""
    if "bing.com/ck/a" not in href:
        return href
    query = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    token = (query.get("u") or [""])[0]
    if token.startswith("a1"):
        token = token[2:]
    if not token:
        return href
    token += "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token).decode("utf-8", "replace")
    except Exception:
        return href


def search_generic(page, self_host, limit):
    """Engine-agnostic fallback: heading that links somewhere external.

    Deliberately ignores container class names so it survives redesigns.
    """
    results = []
    seen = set()
    for m in re.finditer(r'(?is)<h[23][^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page):
        link = html.unescape(m.group(1))
        title = strip_tags(m.group(2))
        if not link.startswith("http") or len(title) < 4 or link in seen:
            continue
        host = urllib.parse.urlparse(link).netloc.lower()
        if self_host in host:
            continue
        seen.add(link)
        tail = re.split(r"(?is)<h[23]", page[m.end():m.end() + 1200])[0]
        results.append({"title": title, "url": link, "snippet": strip_tags(tail)[:240]})
        if len(results) >= limit:
            break
    return results


def search_bing(query, limit):
    # cn.bing.com is the China node: far more reliable from mainland China.
    url = "https://cn.bing.com/search?" + urllib.parse.urlencode(
        {"q": query, "setlang": "zh-CN", "count": "30"}
    )
    page = http_get(url)
    results = []
    for block in re.split(r'(?i)<li class="b_algo"[^>]*>', page)[1:]:
        m = re.search(r'(?is)<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m:
            continue
        link = decode_bing_link(html.unescape(m.group(1)))
        title = strip_tags(m.group(2))
        sm = re.search(r"(?is)<p[^>]*>(.*?)</p>", block)
        snippet = strip_tags(sm.group(1)) if sm else strip_tags(block)[:240]
        if title and link.startswith("http"):
            results.append({"title": title, "url": link, "snippet": snippet})
        if len(results) >= limit:
            break
    return results or search_generic(page, "bing.com", limit)


def search_baidu(query, limit):
    url = "https://www.baidu.com/s?" + urllib.parse.urlencode({"wd": query, "rn": "20"})
    page = http_get(url)
    results = []
    # "result" alone marks a real result; "result-molecule" (the AI widget) must not match.
    starts = list(re.finditer(r'(?i)<div[^>]+class="result(?![-\w])[^"]*"[^>]*>', page))
    for idx, start in enumerate(starts):
        end = starts[idx + 1].start() if idx + 1 < len(starts) else len(page)
        block = page[start.end():end]
        m = re.search(r'(?is)<h3[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not m:
            continue
        title = strip_tags(m.group(2))
        # Baidu routes clicks through /link?url=...; the real target sits in mu="..."
        # on the wrapper element, which is why the opening tag is kept above.
        real = (re.search(r'\smu="([^"]+)"', start.group(0))
                or re.search(r'\smu="([^"]+)"', block))
        link = html.unescape(real.group(1)) if real else html.unescape(m.group(1))
        sm = (re.search(r'(?is)<div[^>]+class="c-abstract[^"]*"[^>]*>(.*?)</div>', block)
              or re.search(r'(?is)<span[^>]+class="content-right[^"]*"[^>]*>(.*?)</span>', block))
        snippet = (strip_tags(sm.group(1)) if sm
                   else strip_tags(re.sub(r"(?is)<h3.*?</h3>", " ", block))[:240])
        if title and link.startswith("http"):
            results.append({"title": title, "url": link, "snippet": snippet})
        if len(results) >= limit:
            break
    return results or search_generic(page, "baidu.com", limit)


def search_360(query, limit):
    url = "https://www.so.com/s?" + urllib.parse.urlencode({"q": query})
    page = http_get(url)
    results = []
    for block in re.split(r'(?i)<li[^>]+class="res-list"[^>]*>', page)[1:]:
        m = re.search(r'(?is)<h3[^>]*>\s*<a[^>]*>(.*?)</a>', block)
        if not m:
            continue
        title = strip_tags(m.group(1))
        # 360 wraps clicks too, but exposes the real URL in data-mdurl="..."
        real = re.search(r'\sdata-mdurl="([^"]+)"', block)
        href = re.search(r'href="([^"]+)"', block)
        link = html.unescape(real.group(1) if real else (href.group(1) if href else ""))
        sm = re.search(r'(?is)<p[^>]+class="res-desc[^"]*"[^>]*>(.*?)</p>', block)
        snippet = (strip_tags(sm.group(1)) if sm
                   else strip_tags(re.sub(r"(?is)<h3.*?</h3>", " ", block))[:240])
        if title and link.startswith("http"):
            results.append({"title": title, "url": link, "snippet": snippet})
        if len(results) >= limit:
            break
    return results or search_generic(page, "so.com", limit)


def web_search(query, limit=8, engine="auto"):
    """Search the web. engine: auto | bing | baidu | 360."""
    limit = max(1, min(int(limit or 8), 20))
    cache_key = (query.strip().lower(), limit, engine)
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    # Bing first (most reliable here). 360 second. Baidu last: it rate-limits
    # automated queries aggressively and may return a stub page.
    order = ["bing", "360", "baidu"] if engine in ("auto", "", None) else [engine]
    handlers = {"bing": search_bing, "baidu": search_baidu, "360": search_360}
    notes = []
    payload = None
    for eng in order:
        handler = handlers.get(eng)
        if handler is None:
            notes.append("unknown engine: %s" % eng)
            continue
        wait = _MIN_ENGINE_INTERVAL - (time.time() - _LAST_CALL.get(eng, 0.0))
        if wait > 0:
            time.sleep(wait)
        try:
            found = handler(query, limit)
        except Exception as exc:
            _LAST_CALL[eng] = time.time()
            notes.append("%s failed: %s" % (eng, exc))
            continue
        _LAST_CALL[eng] = time.time()
        if found:
            payload = {"engine": eng, "query": query, "count": len(found),
                       "results": found, "notes": notes}
            break
        notes.append("%s returned no results" % eng)
    if payload is None:
        payload = {"engine": None, "query": query, "count": 0,
                   "results": [], "notes": notes}
    else:
        _CACHE[cache_key] = payload
    return payload


def fetch_page(url, max_chars=8000):
    """Fetch a URL and return readable text (or a friendly error payload)."""
    max_chars = max(500, min(int(max_chars or 8000), 60000))
    try:
        page = http_get(url)
    except Exception as exc:
        return {"url": url, "title": "", "truncated": False,
                "error": str(exc), "text": ""}
    text = strip_tags(page)
    title = ""
    tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    if tm:
        title = strip_tags(tm.group(1))
    truncated = len(text) > max_chars
    return {"url": url, "title": title, "truncated": truncated,
            "error": None, "text": text[:max_chars]}


def render_search(payload):
    if not payload["results"]:
        detail = "; ".join(payload.get("notes") or []) or "no results"
        return "No results for %r (%s)" % (payload["query"], detail)
    lines = ["Search results (%s), query=%r:" % (payload["engine"], payload["query"])]
    for i, item in enumerate(payload["results"], 1):
        lines.append("%d. %s" % (i, item["title"]))
        lines.append("   %s" % item["url"])
        if item["snippet"]:
            lines.append("   %s" % item["snippet"])
    if payload.get("notes"):
        lines.append("(notes: %s)" % "; ".join(payload["notes"]))
    return "\n".join(lines)


def render_fetch(payload):
    if payload.get("error"):
        return ("Failed to fetch %s: %s\n"
                "Some sites are unreachable from this network - try another search result."
                % (payload["url"], payload["error"]))
    head = "Fetched %s%s" % (payload["url"], " [truncated]" if payload["truncated"] else "")
    if payload["title"]:
        head += "\nTitle: %s" % payload["title"]
    return head + "\n\n" + payload["text"]


TOOLS = [
    {
        "name": "web_search",
        "description": ("Search the web and return ranked results (title, URL, snippet). "
                        "Use this for current information, news, library versions, error messages, "
                        "or anything not in the local code. Follow up with fetch_page to read a result."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query."},
                "limit": {"type": "integer", "description": "Max results (1-20, default 8)."},
                "engine": {"type": "string", "enum": ["auto", "bing", "baidu", "360"],
                           "description": "Search engine. Default auto (Bing, then Baidu, then 360)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_page",
        "description": ("Fetch a URL and return its readable text. Use after web_search "
                        "to read the full content of a promising result."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http(s) URL to fetch."},
                "max_chars": {"type": "integer", "description": "Max characters to return (default 8000)."},
            },
            "required": ["url"],
        },
    },
]

SERVER_INFO = {"name": "web-search", "version": "1.1.0"}
PROTOCOL_VERSION = "2024-11-05"


def call_tool(name, args):
    args = args or {}
    if name == "web_search":
        if not (args.get("query") or "").strip():
            raise ValueError("query is required")
        payload = web_search(args["query"], args.get("limit", 8),
                             args.get("engine", "auto"))
        log_event("web_search query=%r engine=%s results=%d"
                  % (args["query"], payload.get("engine"), payload.get("count", 0)))
        return render_search(payload)
    if name == "fetch_page":
        if not (args.get("url") or "").strip():
            raise ValueError("url is required")
        payload = fetch_page(args["url"], args.get("max_chars", 8000))
        log_event("fetch_page url=%r error=%s chars=%d"
                  % (args["url"], bool(payload.get("error")), len(payload["text"])))
        return render_fetch(payload)
    log_event("unknown tool requested: %s" % name)
    raise ValueError("unknown tool: %s" % name)


def handle(msg):
    """Return a response dict, or None for notifications."""
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        try:
            text = call_tool(params.get("name"), params.get("arguments"))
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": "Error: %s" % exc}], "isError": True}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if msg_id is None:
        return None  # notification (e.g. notifications/initialized)
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": "Method not found: %s" % method}}


def serve():
    log_event("server started pid=%d" % os.getpid())
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        try:
            response = handle(msg)
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": msg.get("id"),
                        "error": {"code": -32603, "message": str(exc)}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def selftest():
    for eng in ("bing", "360", "baidu", "auto"):
        try:
            payload = web_search("Python 3.13 新特性", 3, eng)
        except Exception as exc:
            print("== engine=%-6s EXCEPTION %s" % (eng, exc))
            continue
        print("== engine=%-6s results=%d engine_used=%s notes=%s"
              % (eng, payload["count"], payload["engine"], payload["notes"]))
        for item in payload["results"][:2]:
            print("   * %s" % item["title"][:60])
            print("     %s" % item["url"][:95])
            print("     %s" % item["snippet"][:95])
        print()
    print("== fetch_page ==")
    print(render_fetch(fetch_page("https://www.baidu.com", 200))[:300])


def main():
    argv = sys.argv[1:]
    if "--selftest" in argv:
        selftest()
        return
    if "--search" in argv:
        i = argv.index("--search")
        query = argv[i + 1] if i + 1 < len(argv) else ""
        engine = argv[argv.index("--engine") + 1] if "--engine" in argv else "auto"
        limit = 8
        if "--limit" in argv:
            try:
                limit = int(argv[argv.index("--limit") + 1])
            except Exception:
                pass
        print(render_search(web_search(query, limit, engine)))
        return
    if "--fetch" in argv:
        i = argv.index("--fetch")
        url = argv[i + 1] if i + 1 < len(argv) else ""
        print(render_fetch(fetch_page(url)))
        return
    if "--help" in argv or "-h" in argv:
        print("CLI usage:")
        print('  --search "query" [--engine auto|bing|baidu|360] [--limit N]')
        print("  --fetch URL")
        print("  --selftest")
        print("(no arguments = run as an MCP stdio server)")
        return
    serve()


if __name__ == "__main__":
    main()
