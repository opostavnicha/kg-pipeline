"""Split each PDF in ./pdfs into sections, map headings to canonical types, and pull out tables.

Headings come from the PDF outline (get_toc) when there is one; otherwise they
are detected from typography: bold rows at or above body size that look like
"I. TITLE", "A. Title", "ANNEX 1: Title", "ANNEX: Title" or "DATASHEET", plus unnumbered
bold rows whose text is an entry in the document's printed table of contents.

Tables come from page.find_tables(). Their areas are excluded from section text
and each table is attached to the section it appears in. Prints a summary table
per PDF, runs corpus-wide validation checks, and writes the full sections
(text + tables) and validation flags to out/<pdf stem>.json.

Usage: python -m kg_pipeline.sections [pdf_dir] [section_types.json] [out_dir]
"""

import json
import re
import sys
from collections import Counter, defaultdict
from statistics import median
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pymupdf

from kg_pipeline import paths

BOLD = 1 << 4
ROW_TOLERANCE = 2.0  # pt: spans whose bottoms are this close share a row
COLUMN_GAP = 60.0  # pt: a wider horizontal gap splits a row into segments
BOILERPLATE_SHARE = 0.3  # rows repeated on this share of pages are headers/footers
RULE_THICKNESS = 2.0  # pt: drawings thinner than this are table rules
MIN_RULES = 2  # a table needs this many horizontal or vertical rules
MIN_TABLE_CELLS = 2  # a table needs this many non-empty cells
TOC_PAGE_LEADERS = 3  # a page with this many dot-leader rows is a printed table of contents
PROJECT_ID_RE = re.compile(r"\bP\d{6}\b")
PHASE_RE = re.compile(r"\bphase\s+([IVX]+|\d+)\b", re.IGNORECASE)
LEADER_RE = re.compile(r"\.{4,}|(?:\. ){3,}")
RESULTS_WORDS_RE = re.compile(r"\b(baselines?|targets?|indicators?)\b", re.IGNORECASE)
MAX_KEY_RISK_TABLES = 2
SIZE_OUTLIER_FACTOR = 3
MIN_SIZE_SAMPLES = 3  # a subtype (else its coarse type) needs this many non-empty sections for a size median
MIN_RESULTS_WORDS = 3  # results keywords in a key_risks section before it is flagged
OTHER = "other"  # type for sections with no pattern match and no typed ancestor
# Cover-page titles, matched with whitespace removed (covers sometimes split words, e.g. "PRO GRAM").
DOC_TYPES = {
    "PROJECTAPPRAISALDOCUMENT": "project_appraisal_document",
    "PROGRAMAPPRAISALDOCUMENT": "program_appraisal_document",
    "PROGRAMMEAPPRAISALDOCUMENT": "program_appraisal_document",
    "PROJECTPAPER": "project_paper",
    "PROGRAMDOCUMENT": "program_document",
    "PROGRAMMEDOCUMENT": "program_document",
}
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10}

HEADING_RE = re.compile(
    # "ANNEX 3.1.", "ANNEX 2:", or an unnumbered "ANNEX:" (which then needs its colon/period).
    r"^(?:(?P<roman>[IVX]+)\.|(?P<letter>[A-H])\.|(?P<annex>ANNEX)\b(?:\s*(?P<annex_no>\d+(?:\.\d+)*)\s*[.:]?|\s*[.:]))"
    r"\s*(?P<title>.+)$",
    re.IGNORECASE,
)


@dataclass
class Row:
    page: int  # 1-based
    bbox: tuple[float, float, float, float]
    text: str
    size: float
    bold: bool

    @property
    def pos(self) -> tuple[int, float]:
        return (self.page, self.bbox[1])


@dataclass
class Table:
    page: int
    bbox: tuple[float, float, float, float]
    rows: list[list[str]]

    @property
    def pos(self) -> tuple[int, float]:
        return (self.page, self.bbox[1])


@dataclass
class Section:
    order: int
    level: int
    title_raw: str
    page: int
    type: str | None = None
    subtype: str | None = None
    inherited: bool = False  # type/subtype copied from an ancestor
    scope: str = "main"  # "phase" inside an MPA phase annex
    phase: int | None = None
    phase_project_id: str | None = None
    parent: int | None = None
    children: list[int] = field(default_factory=list)
    text: str = ""
    tables: list[Table] = field(default_factory=list)


# --- rows -------------------------------------------------------------------


def page_rows(page: pymupdf.Page, page_no: int) -> list[Row]:
    """Rebuild visual rows: list numbering ("VI.") is often a separate line from its heading text."""
    spans = [
        s
        for b in page.get_text("dict")["blocks"]
        for line in b.get("lines", [])
        for s in line["spans"]
        if s["text"].strip()
    ]
    spans.sort(key=lambda s: (s["bbox"][3], s["bbox"][0]))

    lines: list[list[dict]] = []
    for s in spans:
        if lines and abs(lines[-1][0]["bbox"][3] - s["bbox"][3]) <= ROW_TOLERANCE:
            lines[-1].append(s)
        else:
            lines.append([s])

    rows = []
    for line in lines:
        line.sort(key=lambda s: s["bbox"][0])
        segment = [line[0]]
        for s in line[1:]:
            if s["bbox"][0] - segment[-1]["bbox"][2] > COLUMN_GAP:
                rows.append(make_row(segment, page_no))
                segment = []
            segment.append(s)
        rows.append(make_row(segment, page_no))
    return rows


def make_row(spans: list[dict], page_no: int) -> Row:
    return Row(
        page=page_no,
        bbox=(
            min(s["bbox"][0] for s in spans),
            min(s["bbox"][1] for s in spans),
            max(s["bbox"][2] for s in spans),
            max(s["bbox"][3] for s in spans),
        ),
        text=re.sub(r"\s+", " ", " ".join(s["text"] for s in spans)).strip(),
        size=max(s["size"] for s in spans),
        bold=all(s["flags"] & BOLD for s in spans),
    )


def boilerplate_key(text: str) -> str:
    return re.sub(r"[\d\s]+", " ", text).strip().lower()


def drop_boilerplate(rows: list[Row], page_count: int) -> list[Row]:
    pages_with = Counter(key for key, _ in {(boilerplate_key(r.text), r.page) for r in rows})
    threshold = max(3, BOILERPLATE_SHARE * page_count)
    return [r for r in rows if pages_with[boilerplate_key(r.text)] < threshold]


# --- tables -----------------------------------------------------------------


def clean_cell(cell: str | None) -> str:
    return re.sub(r"\s+", " ", cell or "").strip()


def page_tables(page: pymupdf.Page, page_no: int) -> list[Table]:
    """find_tables() also fires on shaded prose (per-line filled boxes); keep only tables drawn with rules."""
    found = page.find_tables().tables
    if not found:
        return []
    drawings = [pymupdf.Rect(d["rect"]) for d in page.get_drawings()]
    tables = []
    for t in found:
        area = pymupdf.Rect(t.bbox)
        inside = [r for r in drawings if r.intersects(area)]
        h_rules = sum(1 for r in inside if r.height <= RULE_THICKNESS and r.width > RULE_THICKNESS)
        v_rules = sum(1 for r in inside if r.width <= RULE_THICKNESS and r.height > RULE_THICKNESS)
        rows = [[clean_cell(c) for c in row] for row in t.extract()]
        cells = sum(1 for row in rows for c in row if c)
        if cells >= MIN_TABLE_CELLS and max(h_rules, v_rules) >= MIN_RULES:
            tables.append(Table(page_no, tuple(t.bbox), [row for row in rows if any(row)]))
    return tables


def in_table(row: Row, tables: list[Table]) -> bool:
    cx, cy = (row.bbox[0] + row.bbox[2]) / 2, (row.bbox[1] + row.bbox[3]) / 2
    return any(t.page == row.page and t.bbox[0] <= cx <= t.bbox[2] and t.bbox[1] <= cy <= t.bbox[3] for t in tables)


# --- headings ---------------------------------------------------------------


def body_size(rows: list[Row]) -> float:
    chars = Counter()
    for r in rows:
        chars[round(r.size, 1)] += len(r.text)
    return chars.most_common(1)[0][0]


def is_toc_line(text: str) -> bool:
    return "...." in text or ". . ." in text


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_numbering(title).lower())


def printed_toc(rows: list[Row], toc_pages: set[int]) -> list[str]:
    """Entry titles from the printed table of contents, in order; a leader-only row takes the row above."""
    entries = []
    previous = None
    for r in rows:
        if r.page not in toc_pages:
            continue
        if is_toc_line(r.text):
            title = LEADER_RE.split(r.text)[0].strip()
            if not title and previous is not None and not is_toc_line(previous.text):
                title = previous.text
            if title:
                entries.append(title)
        previous = r
    return entries


def unnumbered_toc_entries(entries: list[str]) -> dict[str, str | None]:
    """Unnumbered TOC entries -> title key of the numbered entry listed just before them (their anchor)."""
    anchors: dict[str, str | None] = {}
    anchor = None
    for title in entries:
        if HEADING_RE.match(title) or title.upper() == "DATASHEET":
            anchor = title_key(title)
        else:
            anchors.setdefault(norm(title), anchor)
    return anchors


def detect_headings(rows: list[Row], tables: list[Table]) -> list[tuple[int, int, str]]:
    """Return (row index, level, title) for rows that are section headings.

    Roman numerals and "ANNEX n" are level 1, "ANNEX n.m" level 2; letters sit one level below the
    last of those, and a letter that restarts (A after F) one level deeper still. An unnumbered bold
    row is a heading only if it is a printed-TOC entry, is not inside a table, and comes after the
    numbered heading listed just before it in the TOC; it sits one level below the current heading.
    """
    base = body_size(rows)
    leaders = Counter(r.page for r in rows if is_toc_line(r.text))
    toc_pages = {page for page, n in leaders.items() if n >= TOC_PAGE_LEADERS}
    pending = unnumbered_toc_entries(printed_toc(rows, toc_pages))
    found: list[tuple[int, int, str]] = []
    seen_keys: set[str] = set()
    parent_level = 1
    last_letter = last_letter_text = None
    last_letter_level = None
    for i, r in enumerate(rows):
        # Skip whole TOC pages: a wrapped entry leaves a row with no dot leader of its own.
        if r.page in toc_pages or is_toc_line(r.text) or len(r.text) > 150 or r.size < base - 0.1:
            continue
        if r.text.upper() == "DATASHEET":
            found.append((i, 1, r.text))
            seen_keys.add(title_key(r.text))
            parent_level, last_letter, last_letter_level = 1, None, None
            continue
        m = HEADING_RE.match(r.text)
        if not r.bold:
            continue
        if not m:
            key = norm(r.text)
            if key in pending and (pending[key] is None or pending[key] in seen_keys) and not in_table(r, tables):
                del pending[key]
                level = (last_letter_level or parent_level) + 1
                found.append((i, level, r.text))
            continue
        if m["roman"] or m["annex"]:
            parent_level = 1 + (m["annex_no"] or "").count(".")
            found.append((i, parent_level, r.text))
            last_letter = last_letter_level = None
        else:
            letter = m["letter"].upper()
            if letter == last_letter and norm(r.text).startswith(norm(last_letter_text)):
                continue  # repeated as a table/figure caption, e.g. "D. Results Chain. PDO: ..."
            restart = last_letter is not None and letter < last_letter
            level = parent_level + (2 if restart else 1)
            found.append((i, level, r.text))
            last_letter_level = level
            if not restart:
                last_letter, last_letter_text = letter, r.text
        seen_keys.add(title_key(r.text))
    return found


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def toc_headings(toc: list, rows: list[Row]) -> list[tuple[int, int, str]]:
    """Locate each outline entry at its row on the target page, falling back to the page's first row."""
    found = []
    for level, title, page in toc:
        on_page = [i for i, r in enumerate(rows) if r.page == page]
        if not on_page:
            continue
        target = norm(title)
        match = next((i for i in on_page if target.startswith(norm(rows[i].text)) or norm(rows[i].text).endswith(target)), None)
        found.append((match if match is not None else on_page[0], level, title))
    return found


# --- sections ---------------------------------------------------------------


def split_sections(doc: pymupdf.Document) -> tuple[list[Section], str]:
    rows: list[Row] = []
    tables: list[Table] = []
    for page_no, page in enumerate(doc, 1):
        rows += page_rows(page, page_no)
        tables += page_tables(page, page_no)
    rows = drop_boilerplate(rows, len(doc))

    toc = doc.get_toc()
    marks = toc_headings(toc, rows) if toc else detect_headings(rows, tables)
    source = "toc" if toc else "font"

    sections = []
    starts = []
    if not marks or marks[0][0] > 0:
        sections.append(Section(0, 0, "(front matter)", 1, type="front_matter", subtype="front_matter"))
        starts.append(0)
    for i, level, title in marks:
        sections.append(Section(len(sections), level, title, rows[i].page))
        starts.append(i)

    # Parent = nearest preceding section with a lower level; front matter (level 0) is nobody's parent.
    stack: list[Section] = []
    for s in sections:
        if s.level == 0:
            continue
        while stack and stack[-1].level >= s.level:
            stack.pop()
        if stack:
            s.parent = stack[-1].order
            stack[-1].children.append(s.order)
        stack.append(s)

    heading_rows = {i for i, _, _ in marks}
    for s, start, end in zip(sections, starts, starts[1:] + [len(rows)]):
        body = [r for j, r in enumerate(rows[start:end], start) if j not in heading_rows and not in_table(r, tables)]
        s.text = "\n".join(r.text for r in body)

    # A table goes to the section in effect at its top edge, unless a heading sits inside it
    # (e.g. a results framework whose first row is the section title).
    for t in tables:
        owner = None
        for s, start in zip(sections, starts):
            if start in heading_rows and in_table(rows[start], [t]):
                owner = s
                break
            if rows[start].pos <= t.pos or s.level == 0:
                owner = s
        owner.tables.append(t)
    return sections, source


# --- classification ---------------------------------------------------------


def strip_numbering(title: str) -> str:
    m = HEADING_RE.match(title.strip())
    return m["title"] if m else title


def load_types(path: Path) -> tuple[list[tuple[str, list[re.Pattern]]], dict[str, str]]:
    config = json.loads(path.read_text())
    subtypes = [(name, [re.compile(p) for p in patterns]) for name, patterns in config["types"].items()]
    group_of: dict[str, str] = {}
    for group, members in config["groups"].items():
        for m in members:
            if m in group_of:
                raise ValueError(f"{path}: subtype {m!r} is in both {group_of[m]!r} and {group!r}")
            group_of[m] = group
    missing = [name for name, _ in subtypes if name not in group_of]
    unknown = [m for m in group_of if m not in config["types"]]
    if missing or unknown:
        raise ValueError(f"{path}: subtypes without a group: {missing}; groups naming unknown subtypes: {unknown}")
    return subtypes, group_of


def classify(title: str, subtypes: list[tuple[str, list[re.Pattern]]]) -> str | None:
    key = norm(re.sub(r"\s*-\s*", "-", strip_numbering(title)))
    for name, patterns in subtypes:
        if any(p.search(key) for p in patterns):
            return name
    return None


def assign_types(sections: list[Section], subtypes, group_of: dict[str, str]) -> None:
    """Match each heading; unmatched subsections inherit from a typed parent (parents come first).

    Sections with no match and no typed parent get type "other" (subtype None).
    """
    for s in sections:
        if s.type is not None:
            continue
        s.subtype = classify(s.title_raw, subtypes)
        parent = sections[s.parent] if s.parent is not None else None
        if s.subtype is not None:
            s.type = group_of[s.subtype]
        elif parent is not None and parent.type not in (None, OTHER):
            s.type, s.subtype, s.inherited = parent.type, parent.subtype, True
        else:
            s.type = OTHER


def phase_number(title: str) -> int | None:
    m = PHASE_RE.search(title)
    if not m:
        return None
    return int(m[1]) if m[1].isdigit() else ROMAN.get(m[1].upper())


def mpa_phase_ids(sections: list[Section]) -> dict[int, str]:
    """Phase -> project ID from the MPA framework table: rows like "2 | Tanzania; P508698 | ...".

    Only the framework table is trusted; phase annexes mostly cite other, earlier projects.
    """
    ids: dict[int, str] = {}
    for s in sections:
        if s.subtype != "mpa_framework":
            continue
        for t in s.tables:
            for row in t.rows:
                if row and row[0].isdigit():
                    found = next((m[0] for c in row[1:] if (m := PROJECT_ID_RE.search(c))), None)
                    if found:
                        ids.setdefault(int(row[0]), found)
    return ids


def assign_scope(sections: list[Section]) -> None:
    """Sections inside an MPA phase annex get scope "phase", the phase number and, where stated, its project ID."""
    ids = mpa_phase_ids(sections)
    for s in sections:
        parent = sections[s.parent] if s.parent is not None else None
        if s.subtype == "mpa_phase" and not s.inherited:
            s.scope, s.phase = "phase", phase_number(s.title_raw)
            title_id = PROJECT_ID_RE.search(s.title_raw)
            s.phase_project_id = ids.get(s.phase) or (title_id[0] if title_id else None)
        elif parent is not None and parent.scope == "phase":
            s.scope, s.phase, s.phase_project_id = "phase", parent.phase, parent.phase_project_id


def document_project_id(doc: pymupdf.Document) -> str | None:
    """The operation's own ID, from the running page header, e.g. "... (P508698)"."""
    pages_with = Counter()
    for page in doc:
        pages_with.update(set(re.findall(r"\((P\d{6})\s*\)", page.get_text())))
    if not pages_with:
        return None
    pid, n = pages_with.most_common(1)[0]
    return pid if n >= BOILERPLATE_SHARE * len(doc) else None


def document_type(doc: pymupdf.Document) -> str:
    """Title on the cover page, e.g. "PROJECT APPRAISAL DOCUMENT" -> project_appraisal_document."""
    cover = re.sub(r"\s+", "", doc[0].get_text()).upper()
    hits = [(cover.find(title), kind) for title, kind in DOC_TYPES.items() if title in cover]
    return min(hits)[1] if hits else "unknown"


def load_core(path: Path) -> dict[str, list[str]]:
    core = json.loads(path.read_text())["core"]
    if "default" not in core:
        raise ValueError(f"{path}: 'core' needs a 'default' entry")
    return core


# --- validation -------------------------------------------------------------


@dataclass
class Doc:
    pdf: Path
    project_id: str | None
    doc_type: str
    source: str
    sections: list[Section]
    flags: list[dict] = field(default_factory=list)


def section_size(s: Section) -> int:
    return len(s.text) + sum(len(c) for t in s.tables for row in t.rows for c in row)


def validate(docs: list[Doc], core: dict[str, list[str]]) -> None:
    """Flag likely extraction errors. Size medians are per coarse type across the whole corpus."""
    by_subtype, by_type = defaultdict(list), defaultdict(list)
    for d in docs:
        for s in d.sections:
            if section_size(s) > 0:
                if s.subtype:
                    by_subtype[s.subtype].append(section_size(s))
                by_type[s.type].append(section_size(s))
    subtype_medians = {k: median(v) for k, v in by_subtype.items() if len(v) >= MIN_SIZE_SAMPLES}
    type_medians = {k: median(v) for k, v in by_type.items() if len(v) >= MIN_SIZE_SAMPLES}

    def size_baseline(s: Section) -> tuple[str, float] | None:
        """Median for the section's subtype, falling back to its coarse type."""
        if s.subtype in subtype_medians:
            return s.subtype, subtype_medians[s.subtype]
        if s.type in type_medians:
            return s.type, type_medians[s.type]
        return None

    for d in docs:
        def flag(kind: str, detail: str, s: Section | None = None) -> None:
            d.flags.append({"kind": kind, "section": None if s is None else s.order,
                            "title": None if s is None else s.title_raw, "detail": detail})

        for s in d.sections:
            if s.subtype == "key_risks" and not s.inherited:
                if len(s.tables) > MAX_KEY_RISK_TABLES:
                    flag("key_risks_tables", f"{len(s.tables)} tables (max {MAX_KEY_RISK_TABLES})", s)
                words = Counter(
                    m.lower().rstrip("s")
                    for text in [s.text, *(c for t in s.tables for row in t.rows for c in row)]
                    for m in RESULTS_WORDS_RE.findall(text)
                )
                if sum(words.values()) >= MIN_RESULTS_WORDS:
                    flag("key_risks_results_words", ", ".join(f"{w}×{n}" for w, n in words.most_common()), s)
            baseline = size_baseline(s)
            if baseline and section_size(s) > SIZE_OUTLIER_FACTOR * baseline[1]:
                flag("oversized", f"{section_size(s):,} chars vs {baseline[0]} median {baseline[1]:,.0f}", s)

        # The operation's own sections: main scope, or the MPA phase annex describing this same project.
        own = [s for s in d.sections if s.scope == "main" or (d.project_id and s.phase_project_id == d.project_id)]
        present = {s.subtype for s in own if not s.inherited}
        for subtype in core.get(d.doc_type, core["default"]):
            if subtype not in present:
                flag("missing_core", f"no {subtype} section for this operation ({d.doc_type})")


# --- output -----------------------------------------------------------------


def print_table(sections: list[Section]) -> None:
    headers = ["order", "type", "subtype", "title_raw", "parent", "scope", "page", "chars", "tables"]
    rows = [
        [
            str(s.order),
            s.type or "?",
            (s.subtype or ("-" if s.type == OTHER else "?")) + ("*" if s.inherited else ""),
            ("  " * max(s.level - 1, 0)) + s.title_raw[:64],
            "" if s.parent is None else str(s.parent),
            "main" if s.scope == "main" else f"phase {s.phase or '?'} {s.phase_project_id or ''}".rstrip(),
            str(s.page),
            f"{len(s.text):,}",
            str(len(s.tables)) if s.tables else "",
        ]
        for s in sections
    ]
    widths = [max(len(h), *(len(r[c]) for r in rows)) for c, h in enumerate(headers)]
    right = {0, 4, 6, 7, 8}

    def fmt(cells):
        return "  ".join(c.rjust(w) if i in right else c.ljust(w) for i, (c, w) in enumerate(zip(cells, widths)))

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))


def write_json(d: Doc, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{d.pdf.stem}.json"
    payload = {
        "pdf": d.pdf.name,
        "project_id": d.project_id,
        "doc_type": d.doc_type,
        "headings_from": d.source,
        "flags": d.flags,
        "sections": [asdict(s) for s in d.sections],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    return path


def main() -> None:
    pdf_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else paths.PDFS
    types_path = Path(sys.argv[2]) if len(sys.argv) > 2 else paths.SECTION_TYPES
    out_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else paths.OUT
    subtypes, group_of = load_types(types_path)
    core = load_core(types_path)

    docs: list[Doc] = []
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        with pymupdf.open(pdf) as doc:
            sections, source = split_sections(doc)
            d = Doc(pdf, document_project_id(doc), document_type(doc), source, sections)
        assign_types(sections, subtypes, group_of)
        assign_scope(sections)
        docs.append(d)

        n_tables = sum(len(s.tables) for s in sections)
        print(f"\n=== {pdf.name}  {d.project_id or '(no project ID)'}  {d.doc_type}  "
              f"({len(sections)} sections, {n_tables} tables, headings from {source})\n")
        print_table(sections)

    validate(docs, core)
    for d in docs:
        write_json(d, out_dir)

    unmatched = [(d.pdf.name, s) for d in docs for s in d.sections if s.type == OTHER]
    inherited = [(d.pdf.name, s) for d in docs for s in d.sections if s.inherited]
    print(f"\n=== Unmatched headings, typed '{OTHER}' ({len(unmatched)})\n")
    for name, s in unmatched:
        print(f"  {name}  p{s.page}  {s.title_raw}")
    print(f"\n=== Typed by inheritance from parent ({len(inherited)}, marked * above)\n")
    for name, s in inherited:
        print(f"  {name}  p{s.page}  {s.title_raw}  ->  {s.type}/{s.subtype}")
    print(f"\n=== Validation flags ({sum(len(d.flags) for d in docs)})")
    for d in docs:
        if d.flags:
            print(f"\n  {d.pdf.name}  ({d.doc_type})")
            for f in d.flags:
                where = f"[{f['section']}] {f['title'][:50]}: " if f["section"] is not None else ""
                print(f"    {f['kind']:24} {where}{f['detail']}")
    print(f"\n  -> {out_dir}/")


if __name__ == "__main__":
    main()
