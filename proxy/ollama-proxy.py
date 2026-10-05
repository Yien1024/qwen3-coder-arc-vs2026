#!/usr/bin/env python3
"""Local proxy that injects web-search results into VS 2026 <-> Ollama chat.

VS 2026's MCP enable UI is broken in this build (servers show UserDisabled
with no way to turn them on). Instead of fighting MCP, this proxy sits
between VS and Ollama and augments every chat request with fresh web-search
results, so the local model can answer with current information.

Flow:
    VS  --POST /v1/chat/completions-->  proxy:11435  -->  Ollama:11434
                                          |
                                          +-- web_search(last_user_msg)
                                          +-- inject results as system msg

Usage:
    python ollama-proxy.py
    python ollama-proxy.py --port 11435 --upstream 11434

In the chat, add "[no-search]" anywhere in your message to skip the search
for that turn (useful for pure coding questions).
"""

import sys
import os
import re
import json
import time
import hashlib
import datetime
import importlib.util
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import urllib.error
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))

# Reuse the battle-tested search engine from the MCP server (no copy-paste).
_spec = importlib.util.spec_from_file_location(
    "wsm", os.path.join(HERE, "web-search-mcp.py"))
wsm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wsm)
web_search = wsm.web_search
render_search = wsm.render_search
fetch_page = wsm.fetch_page
render_fetch = wsm.render_fetch

UPSTREAM = "http://127.0.0.1:11434"
LISTEN_PORT = 11435
NOSEARCH_TOKEN = "[no-search]"
SEARCH_TOKEN = "[search]"          # force a search for this turn
LOG_PATH = os.path.join(HERE, "ollama-proxy.log")

# --- Relevance gating ------------------------------------------------------
# Only results scoring >= RELEVANCE_MIN get injected. Measured motivation: the
# whole-sentence query "Python报错 ModuleNotFoundError: No module named requests
# 怎么解决" returned python.org's homepage and a "how to install Python" tutorial,
# and the old code injected that tutorial as "the content page" while telling the
# model to quote it verbatim. One relevant result beats six noisy ones.
RELEVANCE_MIN = 0.30
# Realtime intent (news / price / latest version) relaxes the gate. Measured:
# "今天有什么重要的AI科技新闻" scored below 0.30 on all 8 Bing results and got
# emptied, even though those 8 were the only sources that could answer it.
RELEVANCE_MIN_REALTIME = 0.15
MAX_INJECTED = 4
FETCH_DEADLINE_S = 8               # hard wall-clock cap on the "fetch one page" step
# Reuse the injected block when the same question comes back. Measured need: in
# VS agent mode every tool round-trip re-sends the same user message, so a
# 3-round conversation ran the whole search 3 times (log: 7->8->9->10).
BLOCK_TTL_S = 300
_MAX_CACHE_ENTRIES = 64
_BLOCK_CACHE = {}

# Keywords that signal the user wants Bilibili-specific info.
_BILIBILI_KEYWORDS = ("b站", "bilibili", "哔哩哔哩", "B站", "Bilibili")


def _is_content_url(url):
    """Return True if the URL looks like a content page (not a site homepage).

    Homepages (https://x.com/, https://x.com, https://x.com/home) rarely have
    the answer; skip them so we fetch real article / product pages instead.
    """
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    path = (p.path or "/").rstrip("/")
    # Root path or common homepage markers -> not a content page.
    if path in ("", "/", "/home", "/index", "/index.html", "/index.htm"):
        return False
    return True


# Content farms / redirector hosts. The old _tech_score rated csdn.net exactly
# the same as stackoverflow.com (both 10 points), which promoted a "how to
# install Python" tutorial to rank 1 for a module-import error. Dropped outright.
_LOW_QUALITY_HOSTS = (
    "csdn.net", "jianshu.com", "51cto.com", "cnblogs.com", "360doc.com",
    "baidu.com", "so.com",
)
# High-confidence sources get a small tie-break bonus only, never a re-rank.
_TRUSTED_HOSTS = (
    "stackoverflow.com", "stackexchange.com", "github.com", "github.io",
    "developer.mozilla.org", "docs.python.org", "docs.oracle.com",
    "learn.microsoft.com", "react.dev", "vuejs.org", "nodejs.org",
    "rust-lang.org", "go.dev", "pkg.go.dev", "spring.io",
    "fastapi.tiangolo.com", "pytorch.org", "huggingface.co",
)


def _host(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""


def _host_matches(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def _query_terms(q):
    """English words (>=3 chars) plus Chinese character bigrams."""
    terms = set(re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{2,}", q.lower()))
    han = re.findall(r"[\u4e00-\u9fff]", q)
    terms |= {a + b for a, b in zip(han, han[1:])}
    return terms


def relevance(result, q):
    """Literal overlap between one result and the query, 0.0 - 1.0."""
    terms = _query_terms(q)
    if not terms:
        return 1.0
    blob = ((result.get("title") or "") + " " + (result.get("snippet") or "")).lower()
    return sum(1 for t in terms if t in blob) / float(len(terms))


def rank_results(results, q):
    """Drop content farms, score by relevance, tie-break towards trusted hosts.

    Returns a list of (score, result) sorted best-first.
    """
    scored = []
    for r in results:
        url = r.get("url", "")
        if not url.startswith("http"):
            continue
        host = _host(url)
        if _host_matches(host, _LOW_QUALITY_HOSTS):
            continue
        score = relevance(r, q)
        if _host_matches(host, _TRUSTED_HOSTS):
            score += 0.15
        scored.append((score, r))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored


def fetch_github_readme(url):
    """Fetch a GitHub repo's README via the API (faster, cleaner than scraping).

    Returns a render_fetch-compatible dict, or None if not a GitHub repo URL.
    """
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return None
    if "github.com" not in p.netloc.lower():
        return None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    api = "https://api.github.com/repos/%s/%s/readme" % (owner, repo)
    try:
        req = urllib.request.Request(api, headers={
            "User-Agent": _BILI_HEADERS["User-Agent"],
            "Accept": "application/vnd.github.v3.raw",
        })
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        text = raw[:3000]
        return {"url": url, "title": "%s/%s README" % (owner, repo),
                "truncated": len(raw) > 3000, "error": None, "text": text}
    except Exception as exc:
        return None


# --- Bilibili WBI signing (official anti-bot mechanism) ---------------------
_WBI_MIXIN_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
_wbi_mixin_key = None
_wbi_key_time = 0.0
_bili_cookie = None
_bili_cookie_time = 0.0

_BILI_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/131.0.0.0 Safari/537.36"),
    "Referer": "https://www.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def _bili_get_cookie():
    """Fetch and cache a buvid3 cookie from bilibili.com (reduces 412)."""
    global _bili_cookie, _bili_cookie_time
    now = time.time()
    if _bili_cookie and (now - _bili_cookie_time) < 86400:
        return _bili_cookie
    import http.cookiejar
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    opener.open(urllib.request.Request("https://www.bilibili.com",
                                       headers=_BILI_HEADERS), timeout=15)
    parts = []
    for c in cj:
        if c.name in ("buvid3", "b_nut"):
            parts.append("%s=%s" % (c.name, c.value))
    # buvid4 is normally set by JS; generate a plausible UUID-based value.
    import uuid
    buvid4 = str(uuid.uuid4()).upper() + "infoc"
    parts.append("buvid4=%s" % buvid4)
    _bili_cookie = "; ".join(parts)
    _bili_cookie_time = now
    return _bili_cookie


def _bili_get_mixin_key():
    """Fetch and cache the WBI mixin key (valid for a long time)."""
    global _wbi_mixin_key, _wbi_key_time
    now = time.time()
    if _wbi_mixin_key and (now - _wbi_key_time) < 3600:
        return _wbi_mixin_key
    headers = dict(_BILI_HEADERS)
    cookie = _bili_get_cookie()
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(
        "https://api.bilibili.com/x/web-interface/nav",
        headers=headers,
    )
    data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
    img = data["data"]["wbi_img"]["img_url"].rsplit("/", 1)[-1].split(".")[0]
    sub = data["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[-1].split(".")[0]
    raw = img + sub
    _wbi_mixin_key = "".join(raw[i] for i in _WBI_MIXIN_TAB)[:32]
    _wbi_key_time = now
    return _wbi_mixin_key


def _bili_sign(params):
    """Sign a param dict with WBI (adds wts + w_rid)."""
    mk = _bili_get_mixin_key()
    params = dict(params)
    params["wts"] = int(time.time())
    query = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + mk).encode()).hexdigest()
    return params


def _bili_api(path, params, referer=None, retries=2):
    """Call a signed Bilibili API and return parsed JSON. Retries on failure."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            signed = _bili_sign(params)
            url = "https://api.bilibili.com" + path + "?" + urllib.parse.urlencode(signed)
            headers = dict(_BILI_HEADERS)
            if referer:
                headers["Referer"] = referer
                headers["Origin"] = referer.rsplit("/", 2)[0]
            cookie = _bili_get_cookie()
            if cookie:
                headers["Cookie"] = cookie
            req = urllib.request.Request(url, headers=headers)
            data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
            # B站 risk control returns code -352 / -412 even with HTTP 200
            if data.get("code") in (-352, -412, 412):
                raise RuntimeError("risk control: %s" % data.get("message"))
            return data
        except Exception as exc:
            last_err = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise last_err


def bilibili_search(keyword):
    """Search Bilibili for a UP主 and return their real video list.

    Uses Bilibili's official API (with WBI signing) instead of scraping
    JS-rendered pages. Returns a formatted string with the UP主's account
    info and the videos they actually uploaded.
    """
    # 1. User search -> get UID, name, fans, video count
    try:
        udata = _bili_api("/x/web-interface/wbi/search/type", {
            "keyword": keyword, "search_type": "bili_user", "page": 1,
        })
    except Exception as exc:
        return "B站用户搜索失败: %s" % exc

    users = (udata.get("data") or {}).get("result") or []
    if not users:
        # Empty results may be due to B站 rate-limiting, not a real absence.
        # Don't let the model claim the UP主 doesn't exist.
        return ("B站搜索暂时没有返回「%s」的用户结果（可能是限流）。"
                "请告知用户：搜索结果不完整，建议直接去B站搜索确认，"
                "不要断言该UP主不存在。") % keyword

    u = users[0]
    mid = u.get("mid")
    uname = u.get("uname", "")
    fans = u.get("fans", 0)
    videos = u.get("videos", 0)
    usign = u.get("usign") or ""

    lines = [
        "【B站UP主信息】（来自B站官方API，数据准确）",
        "用户名: %s" % uname,
        "粉丝数: %s" % fans,
        "视频数: %s" % videos,
    ]
    if usign:
        lines.append("个人签名: %s" % usign)
    lines.append("注意：UP主名称不一定代表其视频内容方向，请勿仅凭用户名推断内容类型。")

    # 2. Try to fetch the UP主's actual video list. B站's space video API is
    # heavily protected (often returns 412/-352 for non-browser clients).
    # If it fails, we silently skip — the account info above is still accurate.
    if mid:
        try:
            vdata = _bili_api("/x/space/wbi/arc/search", {
                "mid": str(mid), "ps": 20, "pn": 1, "order": "pubdate",
                "platform": "web", "web_location": "1550101",
            }, referer="https://space.bilibili.com/%s/video" % mid)
            vlist = ((vdata.get("data") or {}).get("list") or {}).get("vlist") or []
            if vlist:
                lines.append("")
                lines.append("【该UP主发布的视频（按发布时间倒序）】")
                for v in vlist[:15]:
                    title = v.get("title", "")
                    play = v.get("play", 0)
                    length = v.get("length", "")
                    lines.append("- %s（播放:%s, 时长:%s）" % (title, play, length))
        except Exception:
            pass  # space API blocked by risk control; account info is still valid

    return "\n".join(lines)


def log(msg):
    # Timestamps matter: this file mixes runs from several script versions, and
    # without a clock there is no way to tell which line came from which build.
    line = "[proxy %s] %s\n" % (
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:
        pass          # console encoding must never break a request
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


_SYSTEM_TAG_RE = re.compile(
    r"^\s*<(current_datetime|datetime|date|time|system)[^>]*>.*?</\1>\s*$", re.S)


def _msg_text(m):
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for p in c:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text", ""))
        return "".join(parts)
    return ""


# Only the tags VS actually injects are stripped. The previous "delete every tag"
# rule also deleted the user's own content: "把 <T> 改成 List<T> 怎么做" lost the
# <T>, and "<b>粗体</b> 和 <i>斜体</i> 标签怎么用" collapsed to "和  标签怎么用",
# which then became the search query.
_SYS_TAG_ALT = ("current_datetime|system_reminder|sql_tables|tool_response|"
                "system|datetime|reminder|instructions|context|date|time")
# Tag names like "system_reminder" / "sql_tables": lowercase with underscores.
# User prose and code almost never look like that, so this generalises to future
# VS-injected tags without eating the user's markup.
_PAIRED_SYS_TAG_RE = re.compile(
    r"<(?P<tag>%s)(?=[\s>])[^>]*>.*?</(?P=tag)\s*>" % _SYS_TAG_ALT, re.S | re.I)
_PAIRED_SNAKE_TAG_RE = re.compile(
    r"<(?P<tag>[a-z]+(?:_[a-z]+)+)(?=[\s>])[^>]*>.*?</(?P=tag)\s*>", re.S | re.I)
_LOOSE_SYS_TAG_RE = re.compile(
    r"</?(?:%s|[a-z]+(?:_[a-z]+)+)(?=[\s>])[^>]*>" % _SYS_TAG_ALT, re.I)


def _strip_system_tags(text):
    """Remove ONLY VS-injected tags, never the user's own markup."""
    s = _PAIRED_SYS_TAG_RE.sub("", text)
    s = _PAIRED_SNAKE_TAG_RE.sub("", s)
    s = _LOOSE_SYS_TAG_RE.sub("", s)
    return s.strip()


# Meta-questions about the AI's own internet/search capability. Searching
# these returns dictionary garbage; answer directly instead.
_CAPABILITY_Q_RE = re.compile(
    r"(?:你|您)?\s*(?:能|可以|能否|可否|会不会|是否能|是否可以|能不能|可不可以)"
    r"\s*(?:联网|上网|搜索|联网搜索|访问互联网|浏览网页|查资料|搜一下)")


def is_capability_question(text):
    return bool(_CAPABILITY_Q_RE.search(text))


def last_user_text(messages):
    """Return the text of the last real user message, system tags stripped."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        text = _msg_text(m)
        cleaned = _strip_system_tags(text)
        if not cleaned.strip():
            continue
        return cleaned
    return ""


_DATE_PREFIX_RE = re.compile(
    r"^(?:现在|今天|当前|目前|眼下)[是为]?\s*"
    r"(?:\d{4}年\d{1,2}月(?:\d{1,2}日?)?)?"
    r"[，,\s]*")
_TRAIL_INSTR_RE = re.compile(
    r"[？?]\s*(?:请|麻烦|只|仅)?\s*(?:回答|告诉我|说明|输出|给出|用).*$|"+
    r"只?回答[^，。！？\n]*$|"+
    r"(?:请|麻烦)\s*(?:用|以|给出|提供)[^，。！？\n]*$"
)
# Meta-question prefixes about the AI's own capabilities — strip them so the
# search engine focuses on the actual topic, not "can you do X".
_META_PREFIX_RE = re.compile(
    r"^(?:请问|麻烦问下|问下|问一下|帮我(?:查|搜|找|看)?|"
    r"你能(?:否)?|你可以(?:吗|么)?|能不能|可不可以|可以不可以|会不会|"
    r"是否能|是否可以|能否|可否|知不知道|知道吗|"
    r"你知道|你了解|你听说过|你认识|你有没有听说过|你有没有了解过)[，,。.\s]*")
# Sentence-ending question particles that confuse search engines.
_TRAIL_PARTICLE_RE = re.compile(r"[吗么吧呢啊呀]+$")

def clean_query(text):
    """Strip date prefixes, meta-questions, and answer-format instructions
    so the search engine receives the actual topic."""
    # News queries first: strip filler like "今天有什么"/"最近有哪些" before
    # the date-prefix rule eats "今天" and breaks the pattern.
    q = text
    if "新闻" in q or "消息" in q or "动态" in q:
        q = re.sub(r"^(?:今天|现在|目前|最近|近期|这两天|这段时间)\s*"
                   r"(?:有什么|有哪些|有啥|发生了|出了)?\s*", "", q)
        q = re.sub(r"(?:重要的|最新的|近期的|最近的)\s*", "", q)
    q = _DATE_PREFIX_RE.sub("", q)
    q = _META_PREFIX_RE.sub("", q)
    # If a meta-question ends with ？/? and is FOLLOWED BY the real question,
    # drop everything up to and including that question mark. Only truncate
    # when there is content after the mark, otherwise a lone "X?" would be
    # wiped out and we'd fall back to the raw text.
    m_trunc = re.match(r"^[^，。！？\n]*[？?]\s*(.+)$", q, re.S)
    if m_trunc:
        q = m_trunc.group(1)
    q = _TRAIL_INSTR_RE.sub("", q)
    q = _TRAIL_PARTICLE_RE.sub("", q)
    q = q.strip(" ，,。.!！？?\n")
    # Cap length; search engines favour concise queries.
    if len(q) > 60:
        q = q[:60].rstrip(" ，,。.")
    return q or text.strip()


# Literal error / identifier extraction. Measured on this machine: searching the
# whole sentence "Python报错 ModuleNotFoundError: No module named requests 怎么解决"
# returns python.org's homepage and a "how to install Python" tutorial, while
# searching the verbatim error returns 5/5 on-topic fix pages. Same for cv2.
_ERR_RE = re.compile(
    r"((?:[A-Za-z_][A-Za-z0-9_.]*)?(?:Error|Exception|Warning)\b[^\n]{0,120})"
    r"|(\b(?:error|warning)\s+[A-Z]{2}\d{3,5}\b[^\n]{0,80})"
    r"|(\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\([^)\n]{0,80}\))",
    re.I)
# Trailing Chinese question wording ("怎么解决" / "如何修复" ...) is noise to Bing.
_ERR_TAIL_RE = re.compile(
    r"[\s，,。；;：:]*(?:怎么|如何|怎样|怎么办|怎么解决|如何解决|解决方法|"
    r"原因|是什么|请问|谢谢|急)[^\n]*$")
# Trailing dangling English preposition. Measured: "cannot import name getargspec
# from" made Bing return completely unrelated pages (NetEase game sites).
_ERR_DANGLING_RE = re.compile(
    r"\s+(?:from|in|at|on|of|to|with|for|by|line|the|a|an)$", re.I)


def extract_search_query(text):
    """Prefer the verbatim error / identifier over the whole sentence.

    Falls back to clean_query() when the message contains no error-like literal.
    """
    m = _ERR_RE.search(text)
    if m:
        lit = _ERR_DANGLING_RE.sub("", _ERR_TAIL_RE.sub("", m.group(0).strip()))
        lit = lit.strip(" ，,。；;：:'\"")
        if 6 <= len(lit) <= 300:
            return lit
    return clean_query(text)


# Keywords that strongly suggest the user needs real-time / external info.
_REALTIME_KEYWORDS = (
    "最新", "现在", "今天", "目前", "最近", "版本", "价格", "新闻", "消息",
    "官网", "网站", "下载", "安装包", "更新", "发布", "上市", "行情",
    "天气", "汇率", "比分", "直播",
)
# Coding questions that STILL need web search — the model's training data
# (cutoff 2024-06) may be stale for these, or they need a specific fix from
# Stack Overflow / official docs.
_CODE_SEARCH_KEYWORDS = (
    "报错", "错误", "异常", "error", "exception", "traceback", "bug",
    "失败", "不行", "不能用", "无法", "不生效", "不工作",
    "版本", "最新", "更新", "新特性", "新功能", "deprecated", "弃用",
    "文档", "api", "怎么用", "如何使用", "用法", "示例", "教程",
    "配置", "环境", "依赖", "兼容", "升级", "迁移",
)
# Pure coding tasks the model can answer from training data — skip search.
_CODE_PURE_SIGNALS = (
    "写一个", "写段", "实现", "改一下", "优化", "重构", "封装",
    "函数", "算法", "数据结构", "排序", "查找", "遍历",
)
_CODE_PATTERNS = re.compile(
    r"(\.py|\.cs|\.js|\.ts|\.java|\.cpp|\.c|\.h|\.go|\.rs|\.vue|\.jsx|\.tsx|"
    r"\.json|\.xml|\.yaml|\.yml|\.md|"
    r"[A-Za-z]:\\|"
    r"\{.*\}|\(.*\)|=>|def |function |class |import |from .* import|"
    r"Traceback|Exception|Error|error CS\d+|error TS\d+)")

# Pure concept / definition questions. The model answers these from training
# data, so searching anyway just costs ~11s of wall clock and ~1500 prompt
# tokens for nothing. Add "[search]" to the message to force a search anyway.
_PURE_KNOWLEDGE_RE = re.compile(
    r"^(?:请|麻烦)?(?:简单|大概|简要)?\s*"
    r"(?:解释|介绍|说明|讲讲|说说|讲解|科普)(?:一下|下)?\s*"
    r"|^什么是|^啥是|^什么叫|^为啥|^为什么")

# Pure conversational continuations / context-only instructions. They contain
# nothing searchable, yet each one used to trigger a full search (~11s plus
# ~1500 prompt tokens) with a meaningless query like "继续".
_CONTINUATION_RE = re.compile(
    r"^(?:请|麻烦)?\s*(?:继续|接着|往下|再说|好的?|行吧?|嗯+|哦+|是的|对的?|可以|"
    r"ok|okay|go on|continue|还有呢?|还有吗|再来|下一个|然后呢|接下来|"
    r"看一下|看看|分析一下|检查一下|review|谢谢|多谢|辛苦了)"
    r"[。.!！?？~～、\s]*$", re.I)


def needs_web_search(text):
    """Decide whether a user message needs a live web search.

    Strategy:
      - Real-time / external questions -> search.
      - Coding questions about errors, versions, APIs, new features -> search
        (model knowledge may be stale, Stack Overflow has the fix).
      - Pure coding tasks (write an algorithm, refactor) -> skip search.
      - Default -> search.
    """
    if not text or len(text.strip()) < 2:
        return False
    # Pure conversational continuations -> never search.
    if _CONTINUATION_RE.match(text.strip()):
        return False
    tl = text.lower()
    # Explicit real-time or external keywords -> search.
    if any(kw in text for kw in _REALTIME_KEYWORDS):
        return True
    # Website / platform lookups -> search.
    if any(kw in text for kw in ("b站", "bilibili", "哔哩哔哩", "知乎", "微博",
                                  "淘宝", "京东", "github", "github.com",
                                  "官网", "网站", "链接")):
        return True
    # Coding questions that need fresh info (errors, versions, APIs) -> search.
    # Checked BEFORE the entity rule so that "解释一下 React 19 有什么新特性"
    # still searches.
    if any(kw in tl for kw in _CODE_SEARCH_KEYWORDS):
        return True
    # Pure concept / definition questions -> skip (model knows these).
    if _PURE_KNOWLEDGE_RE.search(text) and len(text) <= 40:
        return False
    # Entity / fact questions -> search.
    if re.search(r"(是谁|是什么|介绍(?:一下|下)?|讲讲|说说看|了解|关于).{0,20}$", text):
        return True
    # Pure coding tasks or code-looking text -> skip (model knows these).
    if any(kw in text for kw in _CODE_PURE_SIGNALS):
        return False
    if _CODE_PATTERNS.search(text):
        return False
    # Default: search for anything that isn't obviously a pure coding task.
    return True


# Fetching a page is the one step that can hang for minutes: urlopen's timeout
# does not cover DNS resolution. Measured here: fetching python.org's homepage
# hung for 145s with no response. A bounded pool keeps a hung fetch from
# freezing the reply. (The worker thread is not force-killed, it is just no
# longer waited on.)
_FETCH_POOL = ThreadPoolExecutor(max_workers=2)


def fetch_with_deadline(url, max_chars, seconds=FETCH_DEADLINE_S):
    fut = _FETCH_POOL.submit(fetch_page, url, max_chars)
    try:
        return fut.result(timeout=seconds)
    except FuturesTimeout:
        log("fetch deadline exceeded (%ss), skipping: %s" % (seconds, url))
        return None
    except Exception as exc:
        log("fetch failed: %r" % exc)
        return None


def _append_block(messages, block):
    """Append an injected block to the last real user message."""
    out = list(messages)
    target = -1
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") != "user":
            continue
        if _strip_system_tags(_msg_text(out[i]) or ""):
            target = i
            break
    if target < 0:
        return out
    m = out[target]
    c = m.get("content")
    if isinstance(c, str):
        m["content"] = c + block
    elif isinstance(c, list):
        appended = False
        for p in c:
            if isinstance(p, dict) and p.get("type") == "text":
                p["text"] = p.get("text", "") + block
                appended = True
                break
        if not appended:
            c.append({"type": "text", "text": block})
    return out


def _cache_key(text):
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def _cache_get(key):
    entry = _BLOCK_CACHE.get(key)
    if entry and entry[0] > time.time():
        return entry[1]
    return None


def _cache_put(key, block):
    if len(_BLOCK_CACHE) >= _MAX_CACHE_ENTRIES:
        now = time.time()
        for k in [k for k, v in list(_BLOCK_CACHE.items()) if v[0] <= now]:
            _BLOCK_CACHE.pop(k, None)
        if len(_BLOCK_CACHE) >= _MAX_CACHE_ENTRIES:
            _BLOCK_CACHE.clear()
    _BLOCK_CACHE[key] = (time.time() + BLOCK_TTL_S, block)


def render_sources(results, engine):
    """Numbered source list so the model can cite [S1] / [S2] precisely."""
    lines = ["来源列表（搜索引擎：%s）" % (engine or "?")]
    for i, item in enumerate(results, 1):
        lines.append("[S%d] %s" % (i, (item.get("title") or "").strip()))
        lines.append("     %s" % item.get("url", ""))
        snippet = (item.get("snippet") or "").strip()
        if snippet:
            lines.append("     %s" % snippet[:300])
    return "\n".join(lines)


def _rules_head(now, q, tail):
    return [
        "================ 联网检索结果（本地代理自动注入，非用户输入） ================",
        "检索时间：%s" % now,
        "检索词：%s" % q,
        tail,
    ]


def build_context_block(q, n_results, rendered, fetched, bili_text, weak=False):
    """Injected context: time anchor + citation rule + explicit permission to
    ignore irrelevant results + a required answer shape.

    The old block simply said "请直接引用这些结果回答", which turned the model
    into a quoter of whatever Bing happened to return.
    """
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    head = "已采纳结果数：%d" % n_results
    if weak:
        head += ("（**低置信度**：这些结果与问题的字面相关性很弱，很可能文不对题；"
                 "能不用就不用，用了必须说明这是弱证据）")
    parts = _rules_head(now, q, head)
    parts += [
        "",
        "【使用规则 —— 必须遵守】",
        "1. 下面是可能相关的外部网页摘录，可能过时、片面、甚至与问题无关。",
        "2. 只在与问题确实相关时使用；明显不相关就忽略，并说明「检索结果与问题无关」。",
        "3. 引用具体事实时标注来源编号（如 [S1]）。没有来源支撑的内容必须声明为你的推测。",
        "4. 检索结果与你的知识冲突时，明确指出冲突，并说明你采信哪个、为什么。",
        "5. 不要整段复述网页原文，只提取与问题相关的结论。",
        "6. 回答顺序：先给判断依据，再给结论；最后列出仍不确定的点。",
        "==============================================================================",
        "",
        "【检索结果】",
        rendered,
    ]
    if fetched:
        parts += ["", "---",
                  "【其中一条结果的网页正文（可能夹带无关内容，只取相关的部分）】",
                  "", fetched]
    if bili_text:
        parts += ["", "---",
                  "【B站官方 API 检索结果（不依赖 Bing 排序，可信度较高）】",
                  "", bili_text]
    return "\n\n---\n" + "\n".join(parts)


def build_empty_context_block(q, bili_text, reason):
    """No relevant results found. Say so instead of injecting noise — this is
    the most important anti-hallucination path in the whole proxy."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    parts = _rules_head(now, q, "结果：**本次检索没有找到与问题相关的内容**（%s）。" % reason)
    parts += [
        "",
        "【使用规则 —— 必须遵守】",
        "1. 明确告诉用户「这次联网没有搜到有用信息」，不要假装检索成功。",
        "2. 然后基于你自己的知识回答，并说明哪些部分可能已过时。",
        "3. **不要编造检索结果，也不要引用任何未提供的链接。**",
        "==============================================================================",
    ]
    if bili_text:
        parts += ["", "【B站官方 API 检索结果（不依赖 Bing 排序，仍可采信）】",
                  "", bili_text]
    return "\n\n---\n" + "\n".join(parts)


def inject_search(messages, query, force=False):
    """Append search results (or a capability confirmation) to the last real
    user message. Appending to the user message prevents VS Copilot's
    dominant system prompt from overriding the injected context."""

    # Capability meta-questions ("can you access the internet?") don't need
    # a real search — results would be garbage. Confirm the capability.
    if is_capability_question(query):
        log("capability question detected, skipping web search")
        block = (
            "\n\n---\n"
            "【系统提示】你已接入互联网，具备实时联网搜索能力。"
            "请直接回答用户：可以联网搜索。然后询问用户想搜索什么具体内容。\n"
        )
        return _append_block(messages, block)

    if not force and not needs_web_search(query):
        log("no web search needed (coding/local question), skipping")
        return messages

    # Verbatim error / identifier first: measured 5/5 on-topic vs 0/5 for the
    # whole sentence.
    q = extract_search_query(query)
    # Same question asked again? In VS agent mode every tool round-trip re-sends
    # the same user message, so without this the whole search ran once per round.
    cache_key = _cache_key(q)
    cached_block = _cache_get(cache_key)
    if cached_block is not None:
        log("search cache HIT, reusing block: %s" % q)
        return _append_block(messages, cached_block)
    log("search query (extracted): %s%s" % (q, "  [forced]" if force else ""))

    try:
        payload = web_search(q, limit=8, engine="auto")
    except Exception as exc:
        payload = {"results": [], "engine": None, "query": q, "count": 0,
                   "notes": ["web_search raised: %r" % exc]}

    raw = payload.get("results") or []
    ranked = rank_results(raw, q)
    # Realtime questions get a relaxed gate: they are exactly the cases where a
    # thin result still beats no result.
    realtime = any(kw in query for kw in _REALTIME_KEYWORDS)
    threshold = RELEVANCE_MIN_REALTIME if realtime else RELEVANCE_MIN
    good = [r for score, r in ranked if score >= threshold][:MAX_INJECTED]
    weak = False

    # Bilibili deep search runs regardless of the Bing gate: it uses B站's
    # official (WBI-signed) API, so its quality does not depend on Bing.
    bili_text = ""
    if any(kw in query for kw in _BILIBILI_KEYWORDS):
        bili_kw = re.sub(
            r"(?:b站|bilibili|哔哩哔哩|B站|Bilibili)上?的?|"
            r"这个?(?:up主|UP主|up主)?|(?:up主|UP主)|"
            r"是什么人|是谁|是什么|介绍一下|介绍下|是谁呀|是谁啊|"
            r"怎么样|如何",
            "", q, flags=re.I
        )
        # Keep only the core name: truncate at comma/question-mark and
        # drop trailing "有多少粉丝/视频/播放" type suffixes.
        bili_kw = re.split(r"[，,。.！？?\n]", bili_kw, maxsplit=1)[0]
        bili_kw = re.sub(
            r"(?:有多少|多少|几个|几|粉丝|视频|播放|关注|人气|是多少|"
            r"有多少人|叫什么|名字)", "", bili_kw
        ).strip()
        if bili_kw:
            log("bilibili deep search for: %s" % bili_kw)
            try:
                bili_text = bilibili_search(bili_kw)
            except Exception as exc:
                bili_text = "Bilibili search failed: %s" % exc

    # --- Gate ---------------------------------------------------------------
    # Never lose the search entirely. If nothing cleared the threshold but some
    # result had at least some literal overlap, keep the single best one and
    # label it low-confidence. Measured motivation: "今天有什么重要的AI科技新闻"
    # and a Latin-heavy tech query both scored below 0.30 on EVERY Bing result
    # while still being the only sources that could answer them.
    if not good and ranked and not bili_text:
        best_score, best = ranked[0]
        if best_score > 0.0:
            good, weak = [best], True

    log("search: engine=%s raw=%d kept=%d thr=%.2f realtime=%s weak=%s"
        % (payload.get("engine"), len(raw), len(good), threshold, realtime, weak))

    if not good:
        if raw:
            reason = "引擎返回 %d 条，全部低于相关性阈值 %.2f" % (len(raw), threshold)
        else:
            reason = "搜索引擎没有返回结果（%s）" % (
                "; ".join(payload.get("notes") or []) or "no notes")
        log("RELEVANCE GATE: injecting a 'nothing relevant' notice — %s" % reason)
        empty_block = build_empty_context_block(q, bili_text, reason)
        _cache_put(cache_key, empty_block)     # don't re-search a dead end either
        return _append_block(messages, empty_block)

    # --- Fetch at most ONE content page, under a hard deadline. ------------
    fetched = ""
    for r in good[:1]:
        url = r.get("url", "")
        if not _is_content_url(url):
            log("top result is not a content page, not fetching: %s" % url)
            continue
        log("fetching content result: %s" % url)
        # GitHub -> use API for the README (fast, no JS rendering).
        fp = fetch_github_readme(url)
        if fp is None:
            fp = fetch_with_deadline(url, 2500, FETCH_DEADLINE_S)
        if not fp:
            continue
        text = fp.get("text", "")
        if fp.get("error") or len(text) < 200:
            continue
        fetched = render_fetch(fp)
        break

    rendered = render_sources(good, payload.get("engine"))
    block = build_context_block(q, len(good), rendered, fetched, bili_text, weak)
    _cache_put(cache_key, block)
    return _append_block(messages, block)


def strip_token(messages, token=NOSEARCH_TOKEN):
    """Remove a control token ([no-search] / [search]) from user messages."""
    for m in messages:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            m["content"] = c.replace(token, "").strip()
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and p.get("type") == "text":
                    p["text"] = p.get("text", "").replace(token, "").strip()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))

    def _forward(self, method, path, body, headers):
        url = UPSTREAM + path
        req_headers = {}
        for k, v in headers.items():
            lk = k.lower()
            if lk in ("host", "content-length", "connection",
                      "transfer-encoding", "keep-alive"):
                continue
            req_headers[k] = v
        req = urllib.request.Request(url, data=body, method=method,
                                     headers=req_headers)
        try:
            resp = urllib.request.urlopen(req, timeout=600)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Connection", "close")
            for k, v in e.headers.items():
                if k.lower() in ("transfer-encoding", "connection",
                                 "content-length", "keep-alive"):
                    continue
                self.send_header(k, v)
            self.end_headers()
            chunk = e.read()
            if chunk:
                self.wfile.write(chunk)
            return
        except Exception as exc:
            self.send_response(502)
            self.send_header("Connection", "close")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Proxy error: %s" % exc).encode("utf-8"))
            return

        self.send_response(resp.status)
        self.send_header("Connection", "close")
        for k, v in resp.getheaders():
            lk = k.lower()
            if lk in ("transfer-encoding", "connection",
                      "content-length", "keep-alive"):
                continue
            self.send_header(k, v)
        self.end_headers()
        # Stream Ollama's response straight back (works for SSE / chunked).
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()

    def _handle(self, method):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        path = self.path

        is_chat = (path.endswith("/v1/chat/completions")
                   or path == "/api/chat"
                   or path.endswith("/api/chat"))

        if is_chat and body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                payload = None
            if payload is not None:
                messages = payload.get("messages") or []
                text = last_user_text(messages)
                if text:
                    force = SEARCH_TOKEN in text
                    if force:
                        strip_token(messages, SEARCH_TOKEN)
                        text = last_user_text(messages)
                    if NOSEARCH_TOKEN in text:
                        strip_token(messages, NOSEARCH_TOKEN)
                        log("search skipped (token present)")
                    elif force:
                        log("search forced by [search] token")
                        payload["messages"] = inject_search(
                            messages, text.strip(), force=True)
                    else:
                        payload["messages"] = inject_search(messages, text.strip())
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                # Content-Length will be wrong; we rebuild headers below.

        # Rebuild headers with correct body length.
        new_headers = {}
        for k, v in self.headers.items():
            if k.lower() == "content-length":
                continue
            new_headers[k] = v
        if body:
            new_headers["Content-Length"] = str(len(body))
        self._forward(method, path, body, new_headers)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def main():
    global LISTEN_PORT, UPSTREAM
    argv = sys.argv[1:]
    if "--port" in argv:
        LISTEN_PORT = int(argv[argv.index("--port") + 1])
    if "--upstream" in argv:
        port = argv[argv.index("--upstream") + 1]
        UPSTREAM = "http://127.0.0.1:%s" % port
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return
    server = ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
    log("listening on http://127.0.0.1:%d -> %s" % (LISTEN_PORT, UPSTREAM))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
