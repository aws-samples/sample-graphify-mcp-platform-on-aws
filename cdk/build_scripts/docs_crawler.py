"""Bounded public-docs crawler for url-source graph builds (build plane).

Crawls SOURCE_URL within its own host + path prefix, converts each HTML page
to markdown under /tmp/work/src, and writes a corpus content hash to
/tmp/work/content_hash. The buildspec compares that hash against the repo's
previously published source_hash and skips the whole rebuild when nothing
changed — that in-build comparison IS the url source's change detection (the
poller cannot know whether a site changed without crawling it).

Discovery: sitemap(s) first — robots.txt `Sitemap:` directives, then
/sitemap.xml, sitemap-index recursion capped — filtered to the crawl scope;
when no in-scope sitemap URLs exist, BFS over <a href> links starting at
SOURCE_URL. Both are clamped to CRAWL_MAX_PAGES.

Safety (the build role can reach the registry and the graphs bucket, and the
crawler fetches operator-supplied URLs):
  - every fetched URL must be http(s) on the registered host, and the host
    must resolve ONLY to public unicast IPs — private/loopback/link-local
    (169.254.0.0/16 is where container credentials live) are refused, and the
    check reruns per fetch so a mid-crawl DNS flip is caught;
  - redirects are followed manually (max 3) and re-validated against scope;
  - robots.txt disallow rules are honored; per-page and sitemap byte caps and
    a total time budget bound the build.

Output: <prefix-relative page path>.md with YAML frontmatter carrying
source_url + title (graphify's markdown quick-scan lands frontmatter on the
page node). In-scope links are rewritten to relative .md paths so the
quick-scan mints real cross-page edges. Nothing here may be
run-timestamped — the content hash must be stable when the site is.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path
# Sitemaps are untrusted XML: defusedxml (pinned in cdk/buildspec.py) refuses entity/DTD attacks.
from defusedxml import ElementTree

SOURCE_URL = os.environ["SOURCE_URL"].strip()
MAX_PAGES = max(1, min(int(os.environ.get("CRAWL_MAX_PAGES") or 200), 500))
OUT_DIR = Path(os.environ.get("CRAWL_OUT", "/tmp/work/src"))
HASH_OUT = Path("/tmp/work/content_hash")

UA = "graphify-docs-crawler/1.0"
PAGE_BYTE_CAP = 5 * 1024 * 1024
SITEMAP_BYTE_CAP = 10 * 1024 * 1024
SITEMAP_DOC_CAP = 20          # sitemap-index recursion bound
FETCH_TIMEOUT = 20
FETCH_DELAY = 0.2
MAX_REDIRECTS = 3
TIME_BUDGET_SECONDS = 15 * 60
# Never worth fetching: binary/asset extensions (pages are HTML or markdown).
SKIP_EXT = re.compile(
    r"\.(png|jpe?g|gif|webp|svg|ico|css|js|mjs|map|woff2?|ttf|eot|zip|gz|tgz|bz2|xz|7z|rar|"
    r"pdf|docx?|xlsx?|pptx?|mp[34]|mov|webm|avi|wasm|jar|whl|exe|dmg|bin|iso)$",
    re.IGNORECASE,
)

_DEADLINE = time.monotonic() + TIME_BUDGET_SECONDS


def log(msg: str) -> None:
    print(f"[crawler] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[crawler] FATAL: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


_seed = urllib.parse.urlsplit(SOURCE_URL)
if _seed.scheme not in ("http", "https") or not _seed.hostname:
    die(f"SOURCE_URL must be an http(s) URL: {SOURCE_URL!r}")
HOST = _seed.hostname.lower()
SEED_ORIGIN = f"{_seed.scheme}://{_seed.netloc}"
_seed_path = _seed.path or "/"
# Crawl scope: the "directory" of the registered URL. A trailing slash is a
# section root; a last segment with a known PAGE extension is a file (scope =
# its directory); anything else — including dotted version dirs like /1.18 or
# /v2.0 — is a section name scoping to itself + its subtree. A bare "contains
# a dot" test here silently widened /1.18 to the WHOLE host.
_PAGE_EXT = re.compile(r"\.(html?|xhtml|php|aspx?|md|txt)$", re.IGNORECASE)
if _seed_path.endswith("/"):
    PREFIX = _seed_path
elif _PAGE_EXT.search(_seed_path.rsplit("/", 1)[-1]):
    PREFIX = _seed_path.rsplit("/", 1)[0] + "/"
else:
    PREFIX = _seed_path + "/"


def normalize(url: str) -> str:
    """Canonical page identity: scheme+host+path, no query/fragment.

    Returns "" for URLs the parser rejects (e.g. a malformed IPv6 literal in
    a remote sitemap/Location header) — "" fails in_scope and fetch's scheme
    check, so hostile input skips the URL instead of killing the crawl.
    """
    try:
        s = urllib.parse.urlsplit(url)
        host = (s.hostname or "").lower()
        netloc = host if s.port is None else f"{host}:{s.port}"
        return urllib.parse.urlunsplit((s.scheme, netloc, s.path or "/", "", ""))
    except ValueError:
        return ""


def in_scope(url: str) -> bool:
    s = urllib.parse.urlsplit(url)
    if s.scheme not in ("http", "https") or (s.hostname or "").lower() != HOST:
        return False
    path = s.path or "/"
    return path.startswith(PREFIX) or path == PREFIX.rstrip("/")


_host_check_cache: dict[str, tuple[bool, float]] = {}


def host_is_public(host: str) -> bool:
    """True when every A/AAAA answer is a public unicast address."""
    cached = _host_check_cache.get(host)
    if cached and time.monotonic() - cached[1] < 60:
        return cached[0]
    ok = False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        infos = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip.is_global or ip.is_multicast:
            ok = False
            break
        ok = True
    _host_check_cache[host] = (ok, time.monotonic())
    return ok


def _decode(body: bytes, charset: str) -> str:
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:  # server declared a codec Python doesn't know
        return body.decode("utf-8", errors="replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(url: str, byte_cap: int, require_scope: bool = True) -> tuple[str, bytes, str, str] | None:
    """GET with manual, re-validated redirects.

    Returns (final_url, body, content_type, charset) or None.
    """
    for _ in range(MAX_REDIRECTS + 1):
        if time.monotonic() > _DEADLINE:
            return None
        s = urllib.parse.urlsplit(url)
        if s.scheme not in ("http", "https") or (s.hostname or "").lower() != HOST:
            return None
        if require_scope and not in_scope(url):
            return None
        if not host_is_public(HOST):
            die(f"host {HOST} does not resolve to a public address (refusing to crawl)")
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            resp = _OPENER.open(req, timeout=FETCH_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                location = (exc.headers or {}).get("Location", "")
                exc.close()
                if not location:
                    return None
                try:
                    url = normalize(urllib.parse.urljoin(url, location))
                except ValueError:
                    return None
                continue
            exc.close()
            return None
        except (urllib.error.URLError, OSError, ValueError):
            return None
        with resp:
            body = resp.read(byte_cap + 1)
        if len(body) > byte_cap:
            log(f"skip {url} (> {byte_cap} bytes)")
            return None
        raw_ctype = (resp.headers.get("Content-Type") or "").lower()
        ctype = raw_ctype.split(";")[0].strip()
        charset = "utf-8"
        m = re.search(r"charset=([a-z0-9_.:-]+)", raw_ctype)
        if m:
            charset = m.group(1).strip('"\'')
        return url, body, ctype, charset
    return None


# --- robots.txt ------------------------------------------------------------

_robots = urllib.robotparser.RobotFileParser()
_robots_sitemaps: list[str] = []
_res = fetch(f"{SEED_ORIGIN}/robots.txt", byte_cap=1024 * 1024, require_scope=False)
if _res and _res[2].startswith("text/"):
    lines = _decode(_res[1], _res[3]).splitlines()
    _robots.parse(lines)
    _robots_sitemaps = [
        line.split(":", 1)[1].strip()
        for line in lines
        if line.lower().startswith("sitemap:")
    ]
else:
    _robots.parse([])  # no robots.txt -> allow all


def allowed(url: str) -> bool:
    try:
        return _robots.can_fetch(UA, url)
    except Exception:
        return True


# --- discovery ---------------------------------------------------------------

def sitemap_pages() -> list[str]:
    """In-scope page URLs from the site's sitemap(s), documents capped."""
    # Sub-path docs sites (versioned mkdocs, readthedocs) publish the sitemap
    # under the section prefix, not the origin root — try both.
    queue = list(_robots_sitemaps) or [f"{SEED_ORIGIN}{PREFIX}sitemap.xml", f"{SEED_ORIGIN}/sitemap.xml"]
    seen_docs: set[str] = set()
    pages: list[str] = []
    while queue and len(seen_docs) < SITEMAP_DOC_CAP and len(pages) < MAX_PAGES:
        try:
            sm_url = normalize(urllib.parse.urljoin(SEED_ORIGIN + "/", queue.pop(0)))
        except ValueError:
            continue
        if not sm_url or sm_url in seen_docs:
            continue
        seen_docs.add(sm_url)
        res = fetch(sm_url, byte_cap=SITEMAP_BYTE_CAP, require_scope=False)
        if not res:
            continue
        try:
            root = ElementTree.fromstring(res[1])
        except ElementTree.ParseError:
            continue
        tag = root.tag.rsplit("}", 1)[-1].lower()
        locs = [el.text.strip() for el in root.iter() if el.tag.rsplit("}", 1)[-1].lower() == "loc" and el.text]
        if tag == "sitemapindex":
            queue.extend(locs)
        else:
            for loc in locs:
                u = normalize(loc)
                if in_scope(u) and not SKIP_EXT.search(urllib.parse.urlsplit(u).path) and u not in pages:
                    pages.append(u)
                    if len(pages) >= MAX_PAGES:
                        break
    return pages


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.hrefs.append(v)
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data


# --- conversion --------------------------------------------------------------

_STRIP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|iframe|canvas|template)[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)


def to_markdown(html: str) -> str:
    from markdownify import markdownify  # installed by the buildspec for url builds

    # Pre-strip non-content blocks so their text never leaks into the corpus
    # (same guard graphify's own ingest._html_to_markdown applies).
    html = _STRIP_BLOCKS.sub("", html)
    md = markdownify(html, heading_style="ATX", bullets="-", strip=["img"])
    # Collapse the >2 blank-line runs conversion tends to leave behind.
    return re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"


_SEG_SANITIZE = re.compile(r"[^A-Za-z0-9._-]")


def rel_for(url: str) -> str:
    """Deterministic output path (relative, .md) for a page URL."""
    path = urllib.parse.urlsplit(url).path or "/"
    rel = path[len(PREFIX):] if path.startswith(PREFIX) else path.lstrip("/")
    if rel == "" or rel.endswith("/"):
        rel += "index"
    rel = re.sub(r"\.(html?|xhtml|php|aspx?|md)$", "", rel, flags=re.IGNORECASE)
    parts = []
    for seg in rel.split("/"):
        seg = _SEG_SANITIZE.sub("_", seg)[:80].strip(".") or "_"
        parts.append(seg)
    return "/".join(parts[:12]) + ".md"


_MD_LINK = re.compile(r"(?<!\!)\[([^\]]*)\]\(([^()\s]+)\)")


def rewrite_links(md: str, page_url: str, url_to_rel: dict[str, str], self_rel: str) -> str:
    self_dir = os.path.dirname(self_rel)

    def sub(m: re.Match) -> str:
        target = m.group(2)
        if target.startswith(("#", "mailto:", "data:", "javascript:")):
            return m.group(0)
        try:
            absu = normalize(urllib.parse.urljoin(page_url, target))
        except ValueError:
            return m.group(0)
        rel = url_to_rel.get(absu)
        if rel is None or rel == self_rel:
            return m.group(0)
        return f"[{m.group(1)}]({os.path.relpath(rel, self_dir or '.')})"

    return _MD_LINK.sub(sub, md)


def yaml_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()


def main() -> int:
    seed = normalize(SOURCE_URL)
    if not host_is_public(HOST):
        die(f"host {HOST} does not resolve to a public address (refusing to crawl)")

    pages = sitemap_pages()
    follow_links = not pages
    if pages:
        log(f"sitemap yielded {len(pages)} in-scope page(s)")
        if seed not in pages and in_scope(seed):
            pages.insert(0, seed)
            pages = pages[:MAX_PAGES]
    else:
        log("no usable sitemap; falling back to link-following")
        pages = [seed]

    fetched: dict[str, dict] = {}  # normalized url -> {rel, md/raw, title}
    used_rels: set[str] = set()
    queue = list(pages)
    seen: set[str] = set(queue)
    while queue and len(fetched) < MAX_PAGES:
        if time.monotonic() > _DEADLINE:
            log("time budget hit — proceeding with the pages fetched so far")
            break
        url = queue.pop(0)
        if SKIP_EXT.search(urllib.parse.urlsplit(url).path) or not allowed(url):
            continue
        res = fetch(url, byte_cap=PAGE_BYTE_CAP)
        time.sleep(FETCH_DELAY)
        if not res:
            continue
        final_url, body, ctype, charset = res
        final_norm = normalize(final_url)
        if final_norm in fetched:
            continue
        is_html = ctype in ("text/html", "application/xhtml+xml") or (
            not ctype and body.lstrip()[:1] == b"<"
        )
        is_md = ctype in ("text/markdown", "text/x-markdown") or urllib.parse.urlsplit(
            final_norm
        ).path.lower().endswith(".md")
        if not (is_html or is_md):
            continue

        rel = rel_for(final_norm)
        if rel in used_rels:  # two URLs collapsing onto one path
            rel = rel[:-3] + "-" + hashlib.sha256(final_norm.encode()).hexdigest()[:6] + ".md"
        used_rels.add(rel)

        text = _decode(body, charset)
        if is_html:
            parser = _LinkParser()
            try:
                parser.feed(text)
            except Exception:
                pass
            fetched[final_norm] = {"rel": rel, "html_md": to_markdown(text), "title": re.sub(r"\s+", " ", parser.title).strip()}
            if follow_links:
                for href in parser.hrefs:
                    try:
                        nxt = normalize(urllib.parse.urljoin(final_url, href))
                    except ValueError:
                        continue
                    if nxt not in seen and in_scope(nxt) and not SKIP_EXT.search(urllib.parse.urlsplit(nxt).path):
                        seen.add(nxt)
                        queue.append(nxt)
        else:
            fetched[final_norm] = {"rel": rel, "raw_md": text, "title": ""}

    if not fetched:
        die("crawl produced no pages (site unreachable, out of scope, or robots-disallowed)")

    # Second pass: rewrite in-scope links to relative .md paths, then write.
    url_to_rel = {u: p["rel"] for u, p in fetched.items()}
    hash_lines = []
    for url, page in sorted(fetched.items()):
        rel = page["rel"]
        if "raw_md" in page:
            body_md = page["raw_md"]
        else:
            body_md = rewrite_links(page["html_md"], url, url_to_rel, rel)
        title = page["title"] or rel
        content = (
            f'---\nsource_url: "{yaml_str(url)}"\ntitle: "{yaml_str(title)}"\n---\n\n{body_md}'
        )
        dest = OUT_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        dest.write_bytes(data)
        hash_lines.append(f"{rel}\t{hashlib.sha256(data).hexdigest()}")

    digest = hashlib.sha256("\n".join(sorted(hash_lines)).encode()).hexdigest()
    HASH_OUT.write_text(digest)
    log(f"{len(fetched)} page(s) -> {OUT_DIR} (scope {HOST}{PREFIX}, cap {MAX_PAGES}), hash={digest[:16]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
