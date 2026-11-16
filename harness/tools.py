"""Read-only research tools shared by every agent backend.

CLI harnesses (Claude Code, Codex, Gemini CLI, OpenCode) reach these through the MCP
server in mcp_server.py; API backends (DeepSeek, OpenRouter, Ollama...) call them
in-process. Every tool only reads public data, so an agent can't change anything with them.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

UA = "IdeaHarness/0.2 (research tool)"
MAX_RESULT_CHARS = 20000


class ToolError(Exception):
    pass


# ---------------------------------------------------------------- http

def _check_public(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ToolError("only public http(s) URLs can be fetched")
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ToolError(f"could not resolve {parts.hostname}: {exc}") from None
    for info in infos:
        if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
            raise ToolError("refusing to fetch a private, local or reserved address")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


def _http(url: str, *, data: bytes | None = None, headers: dict | None = None,
          timeout: float = 30, limit: int = 3_000_000) -> tuple[str, str]:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    try:
        with _opener.open(req, timeout=timeout) as resp:
            body = resp.read(limit)
            charset = resp.headers.get_content_charset() or "utf-8"
            return body.decode(charset, "replace"), resp.headers.get_content_type()
    except urllib.error.HTTPError as exc:
        raise ToolError(f"HTTP {exc.code} from {urllib.parse.urlsplit(url).hostname}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"request failed: {exc}") from None


def _json(url: str, **kw):
    text, _ = _http(url, **kw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise ToolError("the service returned something that is not JSON") from None


def _github(path: str, params: dict):
    """GitHub REST GET through the gh CLI (authenticated, higher rate limits) or plain HTTPS."""
    if shutil.which("gh"):
        cmd = ["gh", "api", "-X", "GET", path, "-H", "Accept: application/vnd.github+json"]
        for key, value in params.items():
            cmd += ["-f", f"{key}={value}"]
        for attempt in range(2):
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode == 0:
                return json.loads(proc.stdout)
            err = (proc.stderr or proc.stdout).strip()
            if "rate limit" in err.lower() and attempt == 0:
                time.sleep(30)
                continue
            if "Not Found" in err or "404" in err:
                raise ToolError(f"not found on GitHub: {path}")
            raise ToolError(f"GitHub API error: {err[:300]}")
    query = urllib.parse.urlencode(params)
    return _json(f"https://api.github.com/{path}" + (f"?{query}" if query else ""),
                 headers={"Accept": "application/vnd.github+json"})


def _short(text, n: int = 220) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _limit(value, default: int, top: int) -> int:
    try:
        return max(1, min(top, int(value)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- tools

def github_search_repos(query: str, limit: int = 20, sort: str = "stars") -> dict:
    params = {"q": query, "per_page": _limit(limit, 20, 50)}
    if sort in ("stars", "updated"):
        params.update(sort=sort, order="desc")
    data = _github("search/repositories", params)
    return {
        "total_count": data.get("total_count"),
        "items": [
            {
                "repo": i["full_name"],
                "stars": i.get("stargazers_count"),
                "description": _short(i.get("description")),
                "pushed": (i.get("pushed_at") or "")[:10],
                "created": (i.get("created_at") or "")[:10],
                "archived": i.get("archived", False),
                "topics": (i.get("topics") or [])[:6],
                "url": i.get("html_url"),
            }
            for i in data.get("items", [])
        ],
    }


def github_repo(repo: str) -> dict:
    repo = re.sub(r"^(https?://)?(www\.)?github\.com/", "", repo.strip()).strip("/")
    repo = "/".join(repo.split("/")[:2])
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ToolError("repo must look like owner/name")
    d = _github(f"repos/{repo}", {})
    return {
        "repo": d.get("full_name"),
        "stars": d.get("stargazers_count"),
        "forks": d.get("forks_count"),
        "open_issues": d.get("open_issues_count"),
        "description": _short(d.get("description"), 400),
        "homepage": d.get("homepage"),
        "language": d.get("language"),
        "topics": d.get("topics") or [],
        "license": (d.get("license") or {}).get("spdx_id"),
        "created": (d.get("created_at") or "")[:10],
        "pushed": (d.get("pushed_at") or "")[:10],
        "archived": d.get("archived", False),
        "url": d.get("html_url"),
    }


def github_search_issues(query: str, limit: int = 15) -> dict:
    data = _github("search/issues", {"q": query, "per_page": _limit(limit, 15, 50)})
    return {
        "total_count": data.get("total_count"),
        "items": [
            {
                "title": _short(i.get("title"), 200),
                "repo": "/".join((i.get("repository_url") or "").split("/")[-2:]),
                "state": i.get("state"),
                "comments": i.get("comments"),
                "reactions": (i.get("reactions") or {}).get("total_count"),
                "created": (i.get("created_at") or "")[:10],
                "url": i.get("html_url"),
            }
            for i in data.get("items", [])
        ],
    }


def hn_search(query: str, limit: int = 15, sort: str = "relevance") -> dict:
    endpoint = "search_by_date" if sort == "date" else "search"
    params = urllib.parse.urlencode({"query": query, "tags": "story", "hitsPerPage": _limit(limit, 15, 50)})
    data = _json(f"https://hn.algolia.com/api/v1/{endpoint}?{params}")
    return {
        "total_hits": data.get("nbHits"),
        "items": [
            {
                "title": _short(h.get("title"), 200),
                "points": h.get("points"),
                "comments": h.get("num_comments"),
                "date": (h.get("created_at") or "")[:10],
                "url": h.get("url"),
                "hn_url": f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            }
            for h in data.get("hits", [])
        ],
    }


def package_search(registry: str, query: str, limit: int = 10) -> dict:
    registry = registry.lower().strip()
    n = _limit(limit, 10, 30)
    if registry == "npm":
        params = urllib.parse.urlencode({"text": query, "size": n})
        data = _json(f"https://registry.npmjs.org/-/v1/search?{params}")
        return {"items": [
            {
                "name": o["package"].get("name"),
                "description": _short(o["package"].get("description")),
                "version": o["package"].get("version"),
                "date": (o["package"].get("date") or "")[:10],
                "weekly_downloads": (o.get("downloads") or {}).get("weekly"),
                "repository": (o["package"].get("links") or {}).get("repository"),
            }
            for o in data.get("objects", [])
        ]}
    if registry in ("crates", "crates.io", "cargo"):
        params = urllib.parse.urlencode({"q": query, "per_page": n})
        data = _json(f"https://crates.io/api/v1/crates?{params}")
        return {"items": [
            {
                "name": c.get("name"),
                "description": _short(c.get("description")),
                "downloads": c.get("downloads"),
                "recent_downloads": c.get("recent_downloads"),
                "updated": (c.get("updated_at") or "")[:10],
                "repository": c.get("repository"),
            }
            for c in data.get("crates", [])
        ]}
    if registry == "pypi":
        name = re.sub(r"[^A-Za-z0-9._-]", "", query.strip())
        try:
            info = _json(f"https://pypi.org/pypi/{name}/json")["info"]
        except ToolError:
            return {"note": "PyPI has no search API; this is an exact-name lookup", "found": False}
        return {
            "note": "PyPI has no search API; this is an exact-name lookup",
            "found": True, "name": info.get("name"), "summary": _short(info.get("summary")),
            "version": info.get("version"), "project_urls": info.get("project_urls"),
        }
    raise ToolError("registry must be npm, crates or pypi")


def web_search(query: str, limit: int = 10) -> dict:
    """Best-effort keyless web search: DuckDuckGo Lite, then Bing's RSS results."""
    n = _limit(limit, 10, 25)
    for engine in (_ddg_lite, _bing_rss):
        try:
            items = engine(query)[:n]
        except ToolError:
            items = []
        if items:
            return {"engine": engine.__name__.strip("_"), "items": items}
    raise ToolError("web search returned nothing (the search engines may be blocking automated requests); "
                    "use your own web search tool if you have one, or the GitHub, HN and package tools")


def _bing_rss(query: str) -> list[dict]:
    import xml.etree.ElementTree as ET

    text, _ = _http("https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "format": "rss"}))
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    return [
        {
            "title": _short(item.findtext("title"), 200),
            "url": item.findtext("link") or "",
            "snippet": _short(item.findtext("description"), 300),
        }
        for item in root.iter("item")
    ]


def _ddg_lite(query: str) -> list[dict]:
    page, _ = _http(
        "https://lite.duckduckgo.com/lite/",
        data=urllib.parse.urlencode({"q": query}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    links = re.findall(r"<a([^>]*class=['\"]result-link['\"][^>]*)>(.*?)</a>", page, re.S)
    snippets = re.findall(r"<td[^>]*class=['\"]result-snippet['\"][^>]*>(.*?)</td>", page, re.S)
    items = []
    for idx, (attrs, title) in enumerate(links):
        href = re.search(r"href=['\"]([^'\"]+)['\"]", attrs)
        url = html.unescape(href.group(1)) if href else ""
        if "uddg=" in url:
            url = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("uddg", [url])[0]
        items.append({
            "title": _short(_strip_tags(title), 200),
            "url": url,
            "snippet": _short(_strip_tags(snippets[idx]) if idx < len(snippets) else "", 300),
        })
    return items


def fetch_page(url: str, max_chars: int = 12000) -> dict:
    _check_public(url)
    text, ctype = _http(url)
    title = ""
    if "html" in ctype:
        match = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
        title = _short(_strip_tags(match.group(1)), 200) if match else ""
        text = _html_to_text(text)
    cap = _limit(max_chars, 12000, 40000)
    return {"url": url, "title": title, "content_type": ctype, "truncated": len(text) > cap, "text": text[:cap]}


def _strip_tags(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


def _html_to_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", page)
    text = _strip_tags(page)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ---------------------------------------------------------------- registry

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable


def _params(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


TOOLS = [
    Tool("github_search_repos",
         "Search GitHub repositories and get real star counts, last push dates and descriptions. "
         "Accepts GitHub search syntax, e.g. 'agent undo' or 'topic:cli language:rust stars:>100'.",
         _params({"query": {"type": "string"},
                  "limit": {"type": "integer", "description": "1-50, default 20"},
                  "sort": {"type": "string", "enum": ["stars", "updated", "best-match"]}}, ["query"]),
         github_search_repos),
    Tool("github_repo",
         "Get one GitHub repository's stars, forks, last push, topics and description. repo is 'owner/name' or a GitHub URL.",
         _params({"repo": {"type": "string"}}, ["repo"]),
         github_repo),
    Tool("github_search_issues",
         "Search GitHub issues and pull requests, useful as evidence of demand (people asking for a feature). "
         "Accepts GitHub search syntax, e.g. 'undo is:issue is:open reactions:>5'.",
         _params({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
         github_search_issues),
    Tool("hn_search",
         "Search Hacker News stories (including Show HN launches) with points and comment counts.",
         _params({"query": {"type": "string"}, "limit": {"type": "integer"},
                  "sort": {"type": "string", "enum": ["relevance", "date"]}}, ["query"]),
         hn_search),
    Tool("package_search",
         "Search a package registry: npm or crates (with download counts), or look up an exact PyPI package name.",
         _params({"registry": {"type": "string", "enum": ["npm", "crates", "pypi"]},
                  "query": {"type": "string"}, "limit": {"type": "integer"}}, ["registry", "query"]),
         package_search),
    Tool("web_search",
         "Search the web (best effort, keyless). Prefer your harness's own web search if it has one.",
         _params({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
         web_search),
    Tool("fetch_page",
         "Fetch a public web page and return its readable text.",
         _params({"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"]),
         fetch_page),
]
BY_NAME = {t.name: t for t in TOOLS}


def call_tool(name: str, args: dict | None) -> tuple[str, bool]:
    """Run a tool by name. Returns (text, is_error)."""
    tool = BY_NAME.get(name)
    if tool is None:
        return f"unknown tool '{name}'; available: {', '.join(BY_NAME)}", True
    args = {k: v for k, v in (args or {}).items() if k in tool.parameters["properties"]}
    missing = [k for k in tool.parameters["required"] if k not in args]
    if missing:
        return f"missing argument(s): {', '.join(missing)}", True
    try:
        result = tool.fn(**args)
    except ToolError as exc:
        return str(exc), True
    except Exception as exc:  # a tool failure should never crash an agent session
        return f"{type(exc).__name__}: {exc}", True
    text = json.dumps(result, ensure_ascii=False)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + " …(truncated)"
    return text, False
