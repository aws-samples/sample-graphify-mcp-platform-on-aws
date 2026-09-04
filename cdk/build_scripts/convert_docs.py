"""Convert office/PDF documents in a files-source corpus to markdown (build plane).

graphify's no-LLM quick-scan extracts ONLY markdown (and code) — a corpus of
PDFs classifies as "papers", gets skipped under --code-only, and the build
fails on an empty graph (exactly what happened to the first real files-source
user: one Korean financial-guideline PDF, zero nodes). This pass runs before
extraction and materializes markdown next to each convertible document under
/tmp/work/src:

  report.pdf  -> report.pdf.d/001-<title>.md, 002-<title>.md, ...
                 One PART per section run (see below); inside a part every
                 page keeps a "##### <stem> — p.N" marker so query hits still
                 cite real page numbers. Section titles become ##/###/####
                 headings (topic-named graph nodes).
  spec.docx   -> spec.docx.md    (graphify.detect.docx_to_markdown)
  sheet.xlsx  -> sheet.xlsx.md   (graphify.detect.xlsx_to_markdown)

Why parts instead of one sidecar: graphify slices an oversized single file
into fixed token windows that cut mid-section and attributes everything to one
source_file; a 590-page guide came out as 409 sparse nodes. Splitting at
section headings gives the LLM coherent units, per-section provenance
(source_file = the part), and lets a truncated chunk be bisected at file
boundaries instead of mid-paragraph.

Headings come from three signals, merged by taking the strongest level:
  1. PDF outline (bookmarks) — depth 0 → ##, depth 1 → ###, forced at the
     page they point to.
  2. Font metadata — a line set in a bold face (or clearly larger than the
     body size) and short enough to be a title. Body size = the most common
     font size by character count. Bold lines AT body size are skipped (table
     header cells / inline emphasis).
  3. Text patterns — 제N장/절/조, roman-numeral heads, "N." / "N.N" numbered
     heads (the original Korean-regulation heuristic).

Embedded images (LLM_IMAGES=1 builds only): raster images inside PDFs are
written to <name>.pdf.d/img/pNNNN-KK.png and referenced from the page's
section, so graphify's vision path can describe them. Selection is
deterministic: unique by content hash across the corpus, at least
IMG_MIN_BYTES / IMG_MIN_DIM (drops icons and rules), largest-area first,
with a per-document share of IMG_TOTAL_BUDGET proportional to page count.
Images are downscaled to IMG_MAX_SIDE so each stays well under Bedrock's
5 MB per-image ceiling.

Originals stay in place (the quick-scan skips them; search_code/read_source
refuse binaries anyway). Conversion is DETERMINISTIC — no timestamps — so an
unchanged corpus keeps producing the same graph. Failures (encrypted PDFs,
image-only scans, corrupt files) are skipped with a log line and never fail
the build; the empty-graph assert in post_build still catches a corpus with
nothing extractable at all.

Sidecar frontmatter names the original as `converted_from_file` (NOT
`source_file`: the LLM mirrors that key into its output, and graphify drops
every node attributed to a path that was not dispatched in the chunk — a
24-PDF corpus lost 458 items that way before the rename).
Existing user-authored files are never overwritten (the sidecar name always
appends ".md" / ".d" to the FULL original filename, and an existing
destination is skipped).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

SRC = Path(os.environ.get("CONVERT_DOCS_SRC", "/tmp/work/src"))
PDF_PAGE_CAP = 2000
MD_BYTES_CAP = 10 * 1024 * 1024  # a part bigger than this is truncated

# Part sizing in CHARACTERS of markdown (graphify slices any text unit above
# 20,000 chars into fixed windows — llm.py _FILE_CHAR_CAP — so parts are kept
# under that to reach the model whole). Stronger headings are preferred as
# boundaries: a part closes at the next ## once it holds PART_MIN, at a ###
# past PART_SOFT_MAX, at ANY heading past PART_HARD_MAX, and at the next page
# boundary past PART_PAGE_MAX (a doc with no detectable headings still yields
# page-aligned parts instead of one huge file).
PART_MIN = 8_000
PART_SOFT_MAX = 12_000
PART_HARD_MAX = 15_000
PART_PAGE_MAX = 16_000   # + one page of text stays under the 20k slicing cap
TITLE_MAX = 80           # part titles, and the longest a merged wrapped heading may grow

# Embedded-image extraction (only when the build runs with LLM_IMAGES=1).
IMG_TOTAL_BUDGET = 300   # hard corpus-wide ceiling; the buildspec falls back to text-only above 600 files
IMG_MIN_PER_DOC = 4
IMG_MIN_BYTES = 20_000   # below this it is a logo/icon/rule
IMG_MIN_DIM = 200        # both sides, pixels
IMG_MAX_SIDE = 1280      # downscale longer side to this
# Wall-clock budget per document. One pathological PDF (broken xref, thousands
# of image XObjects) must not stall the whole build; the file is logged as
# FAILED and the rest of the corpus proceeds.
PDF_TIME_BUDGET_S = 600

# Lines that read as section titles inside extracted PDF text. Promoting them
# to markdown headings gives the graph TOPIC-named nodes ("제7장 망분리 ...")
# instead of only generic per-page ones — query_graph matches on node labels,
# so without this a question like "망분리 규제란?" finds nothing even though
# the text contains whole chapters about it. Conservative on purpose: Korean
# 제N장/절/조, roman-numeral heads, and "N. <text>" numbered heads, short
# lines only.
# "1.3. Title" (dotted numbering) is a chapter-grade unit in guides; a bare
# "1. Do this" is as often a procedure step as a heading, so it only gets the
# weakest level and never opens a part on its own.
_RE_CHAPTER = re.compile(r"^\s*(제\s?\d+\s?장|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]\s?[.．]|\d+(\.\d+)+[.．]?\s?[가-힣A-Za-z])")
_RE_SECTION = re.compile(r"^\s*(제\s?\d+\s?절)")
_RE_ARTICLE = re.compile(r"^\s*(제\s?\d+\s?조|\d+[.．]\s?[가-힣A-Za-z])")
# "제14조(정보처리시스템 보호대책) 금융회사는 …" — the article title is the
# parenthesised head; the rest of the line is body text, not heading.
_RE_ARTICLE_HEAD = re.compile(r"^\s*(제\s?\d+\s?조(?:의\s?\d+)?\s?\([^)]{1,60}\))\s*(\S.*)$")
_MONO_RE = re.compile(r"mono|menlo|courier|consolas|code|typewriter", re.I)
# Table-of-contents dot leaders + trailing page numbers ("· · · · · 33").
_TOC_LEADER_RE = re.compile(r"[·.·\s]{4,}\d*\s*$")
_BOLD_RE = re.compile(r"bold|black|heavy|semibold|demibold", re.I)
_SENTENCE_END = (".", "。", ",", ";", ":", "，", "、")
# pypdf emits NUL for glyphs it cannot map (often the space in Korean CID
# fonts); a NUL in a sidecar makes read_source/search_code treat it as binary.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _regex_heading_level(s: str) -> int:
    if not (3 < len(s) < 70):
        return 0
    if _RE_CHAPTER.match(s):
        return 2
    if _RE_SECTION.match(s):
        return 3
    if _RE_ARTICLE.match(s):
        return 4
    return 0


def _font_heading_level(size: float, font: str, s: str, body: float) -> int:
    """Heading level implied by a line's font, 0 when it reads as body text."""
    if not body or size <= 0 or not (5 < len(s) < 120):
        return 0
    if s.endswith(_SENTENCE_END) or re.fullmatch(r"[\W\d_]+", s) or s[0].islower():
        return 0  # sentences, page numbers, separators, wrapped-line tails
    bold = bool(_BOLD_RE.search(font))
    if size >= body * 1.35 and (bold or size >= body * 1.6):
        return 2
    if size > body * 1.05 and bold:
        return 3
    if bold and size < body * 0.98:
        return 4  # bold sub-heads set smaller than the body (common in HTML→PDF renders)
    return 0


def _clean_title(s: str) -> str:
    return _TOC_LEADER_RE.sub("", _CTRL_RE.sub(" ", s)).strip()


def _split_runon_heading(s: str, limit: int = 48) -> tuple[str, str | None]:
    """Cut a numbered heading that pypdf glued to the cell/sentence after it.

    "2.3. 네트워크 보안 관제 수행 클라우드 환경 내 …" → ("2.3. 네트워크 보안 관제 수행", rest).
    Prefers a sentence-ish break, then the last word boundary before `limit`."""
    if len(s) <= limit:
        return s, None
    for sep in (". ", "。", ": ", " - ", " – ", "  "):
        k = s.find(sep, 8)
        if 8 <= k <= limit:
            return s[:k + (1 if sep[0] in ".。" else 0)].strip(), s[k + len(sep):].strip() or None
    k = s.rfind(" ", 8, limit)
    if k < 8:
        return s[:limit].rstrip(), s[limit:].strip() or None
    return s[:k].rstrip(), s[k + 1:].strip() or None


def _slug(title: str) -> str:
    t = unicodedata.normalize("NFC", title).lower()
    t = re.sub(r"[^\w]+", "-", t).strip("-")
    return (t[:60].strip("-")) or "part"


def _yaml_str(s: str) -> str:
    s = _CTRL_RE.sub(" ", s).replace("\t", " ")
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").strip()


def _write_sidecar(orig: Path, kind: str, body: str) -> bool:
    body = body.strip()
    if not body:
        print(f"[convert_docs] skip (no extractable text): {orig.relative_to(SRC)}", file=sys.stderr)
        return False
    dest = orig.with_name(orig.name + ".md")
    if dest.exists():
        print(f"[convert_docs] skip (sidecar exists): {dest.relative_to(SRC)}", file=sys.stderr)
        return False
    rel = orig.relative_to(SRC).as_posix()
    content = (
        f'---\nconverted_from_file: "{_yaml_str(rel)}"\nconverted_from: {kind}\n---\n\n{body}\n'
    )
    data = content.encode("utf-8")
    if len(data) > MD_BYTES_CAP:
        data = data[:MD_BYTES_CAP].decode("utf-8", "ignore").encode("utf-8")
    dest.write_bytes(data)
    print(f"[convert_docs] {rel} -> {dest.name} ({len(data)} bytes)")
    return True


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

@dataclass
class _Line:
    level: int      # 0 = body text, 2..4 = heading, 5 = page marker
    text: str


@dataclass
class _Part:
    title: str
    first_page: int
    last_page: int
    lines: list[str] = field(default_factory=list)
    size: int = 0

    def add(self, s: str) -> None:
        self.lines.append(s)
        self.size += len(s) + 1


def _page_rows(page) -> tuple[str, list[tuple[float, str, str]]]:
    """(plain text, [(font_size, font_name, line)]) for one page."""
    rows: list[tuple[float, str, str]] = []
    cur: list[str] = []
    meta: tuple[float, str] | None = None

    def visitor(text, cm, tm, font_dict, font_size):
        nonlocal cur, meta
        try:
            scale = abs(float(tm[0])) or abs(float(tm[1])) or 1.0
            size = round(abs(float(font_size or 0)) * scale, 1)
        except Exception:  # noqa: BLE001
            size = 0.0
        font = str(font_dict.get("/BaseFont", "")) if font_dict else ""
        segs = str(text).split("\n")
        for k, seg in enumerate(segs):
            if seg:
                # Whitespace-only segments are the spaces BETWEEN words —
                # dropping them glues "Azure Policy설정" together.
                if seg.strip() and meta is None:
                    meta = (size, font)
                cur.append(seg)
            if k < len(segs) - 1 and cur:
                m = meta or (0.0, "")
                rows.append((m[0], m[1], _CTRL_RE.sub(" ", "".join(cur)).strip()))
                cur, meta = [], None

    try:
        text = page.extract_text(visitor_text=visitor) or ""
    except Exception:  # noqa: BLE001 — fall back to the plain extractor
        rows, cur, meta = [], [], None
        text = page.extract_text() or ""
    if cur:
        m = meta or (0.0, "")
        rows.append((m[0], m[1], _CTRL_RE.sub(" ", "".join(cur)).strip()))
    return _CTRL_RE.sub(" ", text), [r for r in rows if r[2]]


def _body_size(pages_rows: list[list[tuple[float, str, str]]]) -> float:
    """Most common font size of paragraph text.

    Weighted by characters over LONG lines in regular, proportional faces —
    tables, code blocks and captions are usually set smaller and would
    otherwise outweigh the prose in a table-heavy guide (and then every bold
    paragraph-size line would read as a heading)."""
    strict: Counter = Counter()
    loose: Counter = Counter()
    for rows in pages_rows:
        for size, font, line in rows:
            if size <= 0:
                continue
            loose[size] += len(line)
            if len(line) >= 40 and not _BOLD_RE.search(font) and not _MONO_RE.search(font):
                strict[size] += len(line)
    # Trust the strict sample only when it is a real share of the text — a
    # slide deck or a form with one long fine-print line must not redefine
    # the body size and turn every other line into a heading.
    pick = strict if sum(strict.values()) >= 0.25 * sum(loose.values()) else loose
    return pick.most_common(1)[0][0] if pick else 0.0


def _outline_headings(reader) -> dict[int, list[tuple[int, str]]]:
    """{page_index: [(level, title)]} from the PDF bookmarks (depth ≤ 1)."""
    out: dict[int, list[tuple[int, str]]] = {}

    def walk(items, depth):
        for it in items:
            if isinstance(it, list):
                if depth < 1:
                    walk(it, depth + 1)
                continue
            try:
                page = reader.get_destination_page_number(it)
                title = _clean_title(str(it.title or ""))
            except Exception:  # noqa: BLE001
                continue
            if page is None or not (1 < len(title) < 160):
                continue
            out.setdefault(int(page), []).append((2 if depth == 0 else 3, title))

    try:
        walk(reader.outline, 0)
    except Exception:  # noqa: BLE001 — a broken outline just means no forced splits
        return {}
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", s)).strip().lower()


def _page_lines(rows: list[tuple[float, str, str]], plain: str, body: float,
                outline: list[tuple[int, str]] | None = None) -> list[_Line]:
    """Turn one page into body lines + heading lines (font, regex, merged wraps).

    Outline (bookmark) titles for this page are resolved IN the text when a
    line starts with them (that line takes the stronger level); only titles
    that do not occur on the page are injected at the top."""
    src = rows if rows else [(0.0, "", ln) for ln in plain.splitlines()]
    out: list[_Line] = []
    prev_font_key: tuple[float, str] | None = None
    pending = [(lvl, title, _norm(title)) for lvl, title in (outline or [])]
    for size, font, raw in src:
        s = raw.strip()
        if not s:
            prev_font_key = None
            continue
        f_level = _font_heading_level(size, font, s, body)
        r_level = _regex_heading_level(s)
        o_level = 0
        ns = _norm(s)
        for i, (lvl, _title, nt) in enumerate(pending):
            if ns.startswith(nt) or nt.startswith(ns) and len(ns) >= 6:
                o_level = lvl
                pending.pop(i)
                break
        level = min(x for x in (f_level, r_level, o_level) if x) if (f_level or r_level or o_level) else 0
        if (f_level and not r_level and not o_level and out and out[-1].level == f_level
                and prev_font_key == (size, font) and len(out[-1].text) + 1 + len(s) <= TITLE_MAX):
            # A wrapped title: same bold face, consecutive lines → one heading,
            # bounded so a run of bold lines cannot fuse into a paragraph.
            out[-1].text = f"{out[-1].text} {_clean_title(s)}"
            continue
        body_line = raw.rstrip()
        if not level and body_line.lstrip().startswith("#"):
            body_line = "\\" + body_line.lstrip()  # a "# comment" line is not a heading
        tail = None
        if level:
            m = _RE_ARTICLE_HEAD.match(s)
            if m:
                s, tail = m.group(1), m.group(2)
            elif r_level:
                # Numbered heads are where pypdf glues the table cell / first
                # sentence onto the title, bold or not.
                s, tail = _split_runon_heading(_clean_title(s))
        if level and len(_clean_title(s)) > 120:
            level = 0  # a "heading" this long is a paragraph the detector misjudged
        out.append(_Line(level, _clean_title(s) if level else body_line))
        if tail:
            out.append(_Line(0, tail))
        prev_font_key = (size, font) if f_level else None
    # Bookmark titles that never appeared in the page text: inject at the top.
    return [_Line(lvl, _clean_title(title)) for lvl, title, _ in pending] + out


def _pdf_image_candidates(reader, n_pages: int) -> list[tuple[int, int, str, int, str]]:
    """[(area, page_index, xobject_name, bytes, sha1)] of usable embedded images.

    Read from the image XObject dictionaries only — /Width, /Height and the
    encoded stream — so NOTHING is decoded here. Decoding every image to
    measure it (the previous approach) took over an hour on a 168-PDF corpus;
    only the chosen few are decoded later by _save_image."""
    cands = []
    for i in range(n_pages):
        try:
            res = reader.pages[i].get("/Resources")
            res = res.get_object() if hasattr(res, "get_object") else res
            xobjs = (res or {}).get("/XObject")
            xobjs = xobjs.get_object() if hasattr(xobjs, "get_object") else xobjs
            if not xobjs:
                continue
            items = list(xobjs.items())
        except Exception:  # noqa: BLE001
            continue
        for name, ref in items:
            try:
                obj = ref.get_object()
                if obj.get("/Subtype") != "/Image":
                    continue
                w, h = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
                if w < IMG_MIN_DIM or h < IMG_MIN_DIM:
                    continue
                raw = getattr(obj, "_data", None)
                if raw is None:
                    raw = obj.get_data()
                if len(raw) < IMG_MIN_BYTES:
                    continue
                cands.append((w * h, i, str(name), len(raw), hashlib.sha1(raw).hexdigest()))
            except Exception:  # noqa: BLE001 — odd XObject, skip
                continue
    return cands


_SEEN_IMAGE_HASHES: set[str] = set()


def _select_images(cands, budget: int) -> dict[int, list[tuple[str, str]]]:
    """{page_index: [(xobject_name, sha1)]} — largest first, unique across the corpus."""
    chosen: dict[int, list[tuple[str, str]]] = {}
    n = 0
    for area, page, name, _size, sha in sorted(cands, key=lambda c: (-c[0], c[1], c[2])):
        if n >= budget:
            break
        if sha in _SEEN_IMAGE_HASHES:
            continue
        _SEEN_IMAGE_HASHES.add(sha)
        chosen.setdefault(page, []).append((name, sha))
        n += 1
    for lst in chosen.values():
        lst.sort()
    return chosen


def _save_image(reader, page: int, name: str, dest: Path) -> bool:
    try:
        img = reader.pages[page].images[name].image
        if img.mode not in ("RGB", "RGBA", "L"):
            img = img.convert("RGB")
        if max(img.size) > IMG_MAX_SIDE:
            img.thumbnail((IMG_MAX_SIDE, IMG_MAX_SIDE))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, format="PNG", optimize=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[convert_docs] image skipped p.{page + 1} {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


def convert_pdf(path: Path, image_budget: int = 0) -> bool:
    from pypdf import PdfReader

    dest_dir = path.with_name(path.name + ".d")
    if dest_dir.exists():
        print(f"[convert_docs] skip (sidecar exists): {dest_dir.relative_to(SRC)}", file=sys.stderr)
        return False
    # Build in a scratch dir and rename at the end: a crash half-way must not
    # leave a partial .d that the next build would skip as "sidecar exists".
    tmp_dir = path.with_name(path.name + ".d.tmp")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    try:
        ok = _convert_pdf_into(path, tmp_dir, image_budget)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    if not ok:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False
    tmp_dir.rename(dest_dir)
    return True


def _convert_pdf_into(path: Path, dest_dir: Path, image_budget: int) -> bool:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    stem = path.stem
    rel = path.relative_to(SRC).as_posix()
    n_pages = min(len(reader.pages), PDF_PAGE_CAP)

    # Pass 1: text + font rows per page (kept in memory: a few MB at most).
    plain: list[str] = []
    rows: list[list[tuple[float, str, str]]] = []
    for i in range(n_pages):
        try:
            t, r = _page_rows(reader.pages[i])
        except Exception:  # noqa: BLE001
            t, r = "", []
        plain.append(t)
        rows.append(r)
    # Embedded images (LLM_IMAGES=1 only): choose now, write while assembling.
    chosen: dict[int, list[tuple[str, str]]] = {}
    if image_budget > 0:
        chosen = _select_images(_pdf_image_candidates(reader, n_pages), image_budget)
    if not any(t.strip() for t in plain) and not chosen:
        print(f"[convert_docs] skip (no extractable text): {rel}", file=sys.stderr)
        return False
    body = _body_size(rows)
    outline = _outline_headings(reader)

    # Pass 2: assemble parts. A part opens with the heading that starts it and
    # closes at the next heading once big enough (see PART_* above).
    parts: list[_Part] = []
    part: _Part | None = None
    n_images = 0

    def open_part(title: str, page_no: int) -> _Part:
        nonlocal part
        title = (title or stem).strip()
        if len(title) > TITLE_MAX:
            title = title[:TITLE_MAX - 1].rstrip() + "…"
        part = _Part(title=title, first_page=page_no, last_page=page_no)
        parts.append(part)
        return part

    def splits_here(level: int) -> bool:
        return bool(level) and part is not None and (
            (level <= 2 and part.size >= PART_MIN)
            or (level <= 3 and part.size >= PART_SOFT_MAX)
            or part.size >= PART_HARD_MAX
        )

    for i in range(n_pages):
        page_no = i + 1
        lines = _page_lines(rows[i], plain[i], body, outline.get(i))
        if not lines and i not in chosen:
            continue
        marker = f"##### {stem} — p.{page_no}"
        marker_written = False

        def emit(text: str) -> None:
            # The page marker is written lazily with the first line that lands
            # in a part, so a page whose top heading opens a new part never
            # leaves a content-free marker (and a wrong pages: range) behind.
            nonlocal marker_written
            if not marker_written:
                part.add(marker)
                part.last_page = page_no
                marker_written = True
            part.add(text)

        def emit_images() -> None:
            nonlocal n_images
            for k, (name, _sha) in enumerate(chosen.get(i, [])):
                img_rel = f"img/p{page_no:04d}-{k + 1:02d}.png"
                if _save_image(reader, i, name, dest_dir / img_rel):
                    n_images += 1
                    emit(f"![{stem} p.{page_no} figure {k + 1}]({img_rel})")

        start = 0
        if part is None or part.size >= PART_PAGE_MAX or (lines and splits_here(lines[0].level)):
            # First page, an overflowing part, or a page-top heading that
            # closes the previous part: open here. A heading at the top of
            # the page becomes the title (not repeated in the body).
            if lines and lines[0].level:
                open_part(lines[0].text, page_no)
                start = 1
            else:
                open_part(stem if part is None else f"{stem} (p.{page_no}~)", page_no)
            marker_written = False
        emit_images()
        for ln in lines[start:]:
            if splits_here(ln.level):
                open_part(ln.text, page_no)
                marker_written = False
                continue  # the heading titles the new part
            emit(f"{'#' * ln.level} {ln.text}" if ln.level else ln.text)
    if len(reader.pages) > PDF_PAGE_CAP and part is not None:
        part.add(f"\n(truncated at {PDF_PAGE_CAP} pages)")
    if not parts:
        print(f"[convert_docs] skip (no extractable text): {rel}", file=sys.stderr)
        return False

    dest_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    width = max(3, len(str(len(parts))))
    for idx, p in enumerate(parts, 1):
        pages = f"{p.first_page}" if p.first_page == p.last_page else f"{p.first_page}-{p.last_page}"
        content = (
            f'---\nconverted_from_file: "{_yaml_str(rel)}"\nconverted_from: pdf\n'
            f"part: {idx}/{len(parts)}\ntitle: \"{_yaml_str(p.title)}\"\npages: \"{pages}\"\n---\n\n"
            f"# {p.title}\n\n" + "\n".join(p.lines) + "\n"
        )
        data = content.encode("utf-8")
        if len(data) > MD_BYTES_CAP:
            data = data[:MD_BYTES_CAP].decode("utf-8", "ignore").encode("utf-8")
        (dest_dir / f"{idx:0{width}d}-{_slug(p.title)}.md").write_bytes(data)
        total += len(data)
    print(f"[convert_docs] {rel} -> {dest_dir.name}/ ({len(parts)} part(s), {total} bytes, {n_images} image(s), "
          f"body font {body}pt, outline {'yes' if outline else 'no'})", flush=True)
    return True


def convert_docx(path: Path) -> bool:
    from graphify.detect import docx_to_markdown

    return _write_sidecar(path, "docx", docx_to_markdown(path))


def convert_xlsx(path: Path) -> bool:
    from graphify.detect import xlsx_to_markdown

    return _write_sidecar(path, "xlsx", xlsx_to_markdown(path))


def _pdf_page_counts(pdfs: list[Path]) -> dict[Path, int]:
    from pypdf import PdfReader

    counts = {}
    for p in pdfs:
        try:
            counts[p] = max(1, min(len(PdfReader(str(p)).pages), PDF_PAGE_CAP))
        except Exception:  # noqa: BLE001
            counts[p] = 1
    return counts


def main() -> int:
    files = [p for p in sorted(SRC.rglob("*")) if p.is_file()]
    pdfs = [p for p in files if p.suffix.lower() == ".pdf"]
    # Only an LLM build with the vision path on consumes the PNGs; a quick-scan
    # build would just carry tens of MB of images nobody reads.
    with_images = os.environ.get("LLM_IMAGES", "0") == "1" and os.environ.get("LLM_EXTRACT", "0") == "1"
    budgets: dict[Path, int] = {}
    if with_images and pdfs:
        counts = _pdf_page_counts(pdfs)
        total_pages = sum(counts.values()) or 1
        budgets = {p: max(IMG_MIN_PER_DOC, round(IMG_TOTAL_BUDGET * c / total_pages)) for p, c in counts.items()}
        # The per-document floor must not breach the corpus ceiling (the
        # buildspec's 600-file cap sits above it): trim the largest shares.
        while sum(budgets.values()) > IMG_TOTAL_BUDGET:
            big = max(budgets, key=lambda k: (budgets[k], counts[k]))
            if budgets[big] <= 1:
                break
            budgets[big] -= 1
        print(f"[convert_docs] embedded-image extraction on: {IMG_TOTAL_BUDGET} across {len(pdfs)} PDF(s), "
              f"{total_pages} pages")
    converted = failed = 0

    def _alarm(_signum, _frame):
        raise TimeoutError(f"document exceeded {PDF_TIME_BUDGET_S}s")

    signal.signal(signal.SIGALRM, _alarm)
    for path in files:
        ext = path.suffix.lower()
        try:
            signal.alarm(PDF_TIME_BUDGET_S)
            if ext == ".pdf":
                ok = convert_pdf(path, budgets.get(path, 0))
            elif ext == ".docx":
                ok = convert_docx(path)
            elif ext == ".xlsx":
                ok = convert_xlsx(path)
            else:
                continue
            if ok:
                converted += 1
        except Exception as exc:  # noqa: BLE001 — one bad document must not fail the build
            failed += 1
            for leftover in (path.with_name(path.name + ".d.tmp"),):
                shutil.rmtree(leftover, ignore_errors=True)
            print(f"[convert_docs] FAILED {path.relative_to(SRC)}: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
        finally:
            signal.alarm(0)
    print(f"[convert_docs] converted {converted} document(s)" + (f", {failed} failed" if failed else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
