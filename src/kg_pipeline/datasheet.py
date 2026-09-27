"""Extract the DATASHEET of each PDF in ./pdfs, cross-check it against the World Bank Projects API,
and write out/<pdf stem>.datasheet.json.

PDF side: project ID (confirmed in the datasheet), report number and date (cover), PDO, financing
sources, implementing agencies, borrower, SORT risk ratings, E&S risk rating, instrument and
modality checkboxes, and for MPA documents the program ID and phases. Tables come from sections.py.

API side (search.worldbank.org Projects API, cached in cache/wb_projects/<version>/): v3 gives name,
country, region, status, board dates, commitments, borrower, agencies, E&S rating and PDO; v2 fills in
lending instrument and practice area, which v3 lacks. (v2 alone misses many projects: it has no record
for recent ones and only a ratings/financials record for some closed ones, e.g. P149095.) Fields where
the PDF and the API disagree go to conflicts[]; zero/empty API values count as missing, not as data.

Derived fields (marked *_source = "derived"): region from country via countries.json, and status from
the PDF's approval/closing dates. They are used when the API has no value, and cross-checked when it does.

Contact details (people, phone numbers, emails) are deliberately not extracted from the PDF, and
staff fields are dropped from API responses before caching.

Usage: python -m kg_pipeline.datasheet [pdf_dir] [out_dir]
"""

import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import pymupdf

from kg_pipeline import paths
from kg_pipeline import sections as S

API_URLS = {
    "v3": "https://search.worldbank.org/api/v3/projects?format=json&fl=*&id={id}",
    "v2": "https://search.worldbank.org/api/v2/projects?format=json&fl=*&id={id}",
}
CACHE_DIR = paths.CACHE / "wb_projects"
ALIASES_PATH = paths.ALIASES
COUNTRIES_PATH = paths.COUNTRIES
ES_RATING_CODES = {"L": "Low", "M": "Moderate", "S": "Substantial", "H": "High"}
TYPES_PATH = paths.SECTION_TYPES
PERSONAL_API_FIELDS = re.compile(r"^team", re.IGNORECASE)  # team lead names/emails: never cached

MARKER = "@#&OPS"
RATING_RE = re.compile(r"\b(Low|Moderate|Substantial|High)\b")
DATE_RE = re.compile(r"\b(\d{1,2}-[A-Z][a-z]{2}-\d{4})\b")
AMOUNT_RE = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")
PROJECT_ID_RE = re.compile(r"^P\d{6}$")
CHECKBOX_RE = re.compile(r"\[\s*(✓|✔|x|X)?\s*\]\s*([^\[]+)")
HEADER_SLACK = 2  # value cells sit at, or up to this many columns left of, their header cell
AMOUNT_TOLERANCE = 0.01  # US$ millions

# Datasheet financing rows that open a group; the rows after them (until the amounts add up) are its parts.
FINANCING_GROUP_RE = re.compile(
    r"^(International Development Association|International Bank for Reconstruction|IDA$|IBRD$|"
    r"Counterpart Funding|Trust Funds|Commercial Financing|Other Sources|Other Financing|Guarantees?\b)",
    re.IGNORECASE,
)
FINANCING_SECTION_RE = re.compile(r"^(World Bank Group Financing|Non-World Bank Group Financing)$", re.IGNORECASE)
COUNTERPART_BORROWER_RE = re.compile(r"^(Borrower/Recipient|National Government|Government)$", re.IGNORECASE)
TOTAL_LABELS = {
    "government_program_cost": r"^Government program Cost",
    "total_operation_cost": r"^Total (Operation|Project) Cost",
    "total_program_cost": r"^Total Program Cost",
    "ipf_component": r"^IPF Component",
    "total_financing": r"^Total Financing",
    "of_which_ibrd_ida": r"^of which IBRD/IDA",
    "financing_gap": r"^Financing Gap",
}
MODALITIES = {
    "mpa": r"Multiphase Programmatic",
    "cerc": r"Contingent Emergency Response Component",
    "sop": r"Series of Projects",
    "pbc": r"Performance-Based Conditions",
    "financial_intermediaries": r"Financial Intermediaries",
    "project_based_guarantee": r"Project-Based Guarantee",
    "deferred_drawdown": r"Deferred Drawdown",
    "apa": r"Alternat\w+ Procurement Arrangements",
    "heis": r"Hands-on",
    "fragile_state": r"^Fragile State",
    "fragile_within_nonfragile": r"Fragile within",
    "small_state": r"Small State",
    "conflict": r"^Conflict",
    "disaster_response": r"Natural or Man-made Disaster",
}
STOP_RE = re.compile(
    r"^(Components|Component Name|Organizations|MPA FINANCING|PROJECT FINANCING|COST & FINANCING|"
    r"MPA Program Development Objective|Proposed (Program |Project )?Development Objective|@#&OPS)",
    re.IGNORECASE,
)


# --- helpers ----------------------------------------------------------------


def cells(row: list[str]) -> list[str]:
    return [c for c in row if c]


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def fold(text: str | None) -> str:
    """Case/accent/punctuation-insensitive form for comparisons."""
    if text is None:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def iso_date(text: str | None) -> str | None:
    if not text:
        return None
    text = text.strip()
    for fmt in ("%d-%b-%Y", "%B %d, %Y", "%B %d %Y", "%Y-%m-%d", "%m/%d/%Y %I:%M:%S %p"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    m = re.match(r"(\d{4}-\d{2}-\d{2})T", text)
    return m[1] if m else None


def amount(text: str) -> float | None:
    text = text.strip()
    if not AMOUNT_RE.match(text):
        return None
    negative = text.startswith("(") or text.startswith("-")
    value = float(text.strip("()-").replace(",", ""))
    return -value if negative else value


def find(rows: list[list[str]], pattern: str, start: int = 0) -> int | None:
    rx = re.compile(pattern, re.IGNORECASE)
    for i in range(start, len(rows)):
        if any(rx.search(c) for c in cells(rows[i])):
            return i
    return None


def owner(headers: dict[int, str], j: int) -> int:
    """Header column for a value in column j: nearest header at or up to HEADER_SLACK columns to the right, else to the left."""
    right = [h for h in headers if j <= h <= j + HEADER_SLACK]
    left = [h for h in headers if h < j]
    return min(right) if right else max(left) if left else min(headers)


def grid(rows: list[list[str]], header_i: int, is_value_row, look_ahead: int = 4) -> tuple[dict[str, str], int | None]:
    """Map a value row to the header row above it by column. A value cell belongs to the nearest header
    at or up to HEADER_SLACK columns to its right, else to the nearest header on its left.
    Returns ({header text: value}, value row index)."""
    headers = {j: c for j, c in enumerate(rows[header_i]) if c}
    for k in range(header_i + 1, min(header_i + 1 + look_ahead, len(rows))):
        if not is_value_row(rows[k]):
            continue
        out: dict[str, str] = {}
        for j, c in enumerate(rows[k]):
            if not c:
                continue
            h = owner(headers, j)
            out[headers[h]] = clean(f"{out.get(headers[h], '')} {c}")
        return out, k
    return {}, None


def pick(values: dict[str, str], pattern: str) -> str | None:
    rx = re.compile(pattern, re.IGNORECASE)
    return next((v for h, v in values.items() if rx.search(h)), None)


class Aliases:
    def __init__(self, path: Path):
        orgs = json.loads(path.read_text())["organizations"]
        self.lookup = {fold(v): canon for canon, variants in orgs.items() for v in [canon, *variants]}

    def __call__(self, name: str | None) -> str | None:
        if not name:
            return None
        return self.lookup.get(fold(name), clean(name))

    def known(self, name: str | None) -> bool:
        return fold(name) in self.lookup


# --- cover ------------------------------------------------------------------


COVER_AMOUNT_RE = re.compile(
    r"PROPOSED\s+(?P<kind>LOAN|CREDIT|GRANT)S?\s+IN THE AMOUNT OF\s+"
    r"(?P<cur>US\$|EUR|SDR|€|[A-Z]{3})\s*(?P<amt>[\d.,]+)\s*(?P<unit>MILLION|BILLION)"
    r"(?:\s*\(\s*US\$\s*(?P<usd>[\d.,]+)\s*(?P<usd_unit>MILLION|BILLION)\s+EQUIVALENT\s*\))?"
    r"(?:\s+EQUIVALENT)?"
    r"(?:\s+FROM\s+(?:THE\s+)?(?P<src>.+?))?(?=\s+(?:AND\s+(?:A\s+)?PROPOSED|TO\s+|FOR\s+(?:THE|AN?)\b|ON\s+)|$)",
    re.IGNORECASE,
)


def parse_cover(doc: pymupdf.Document) -> dict:
    text = clean(doc[0].get_text())
    report = re.search(r"Report\s+No[.:]?\s*:?\s*([A-Z]{0,6}\d[A-Z0-9-]*)", text, re.IGNORECASE)
    when = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b", text, re.IGNORECASE)
    cover_date = iso_date(f"{when[1].title()} {when[2]}, {when[3]}") if when else None
    amounts = []
    for m in COVER_AMOUNT_RE.finditer(text):
        cur = {"US$": "USD", "€": "EUR"}.get(m["cur"].upper(), m["cur"].upper())
        scale = 1000 if m["unit"].upper() == "BILLION" else 1
        value = float(m["amt"].replace(",", "")) * scale
        usd = float(m["usd"].replace(",", "")) * (1000 if (m["usd_unit"] or "").upper() == "BILLION" else 1) if m["usd"] else (value if cur == "USD" else None)
        amounts.append({
            "kind": m["kind"].lower(),
            "currency": cur,
            "amount_millions": value,
            "usd_equivalent_millions": usd,
            "from": clean(m["src"]) if m["src"] else None,
        })
    return {"report_number": report[1] if report else None, "report_label_found": bool(re.search(r"Report\s+No", text, re.I)),
            "date": cover_date, "cover_amounts": amounts}


# --- datasheet ---------------------------------------------------------------


def datasheet_rows(sections: list[S.Section]) -> list[list[str]]:
    ds = next((s for s in sections if s.subtype == "datasheet"), None)
    return [] if ds is None else [row for t in ds.tables for row in t.rows]


def note(missing, field, label_found: bool) -> None:
    missing.append((field, "unparsed" if label_found else "absent"))


def parse_basic(rows, out, missing):
    hi = find(rows, r"^(Operation|Project) Name$")
    name_hi = hi
    if hi is not None:
        vals, _ = grid(rows, hi, lambda r: cells(r) and not re.search(r"Beneficiar", " ".join(cells(r)), re.I))
        out["name"] = pick(vals, r"Name")
        out["country"] = pick(vals, r"Beneficiar|Countr|^Project$")
    hi = find(rows, r"^(Operation|Project) ID$")
    id_headers = " ".join(cells(rows[hi])) if hi is not None else ""
    if hi is not None:
        vals, vi = grid(rows, hi, lambda r: any(PROJECT_ID_RE.match(c) for c in r))
        out["project_id_datasheet"] = next((v for v in vals.values() if PROJECT_ID_RE.match(v)), None)
        instrument = pick(vals, r"Financing Instrument") or ""
        # The instrument cell wraps onto following rows ("Financing (IPF) – / Contingent Emergency / ...");
        # take only the cells under the instrument column (Iraq's next column, "Process", wraps too).
        headers = {j: c for j, c in enumerate(rows[hi]) if c}
        col = next((j for j, c in headers.items() if re.search(r"Financing Instrument", c, re.I)), None)
        for k in range(vi + 1, min(vi + 6, len(rows))) if vi is not None and col is not None else []:
            row = cells(rows[k])
            if not row or row[0].startswith(MARKER) or re.search(r"Modalities", row[0], re.I):
                break
            instrument += " " + " ".join(c for j, c in enumerate(rows[k]) if c and owner(headers, j) == col)
        out["instrument_text"] = clean(instrument) or None
        out["instrument"] = (
            "PforR" if re.search(r"Program-for-Results", instrument, re.I)
            else "DPF" if re.search(r"Development Policy", instrument, re.I)
            else "IPF" if re.search(r"Investment Project", instrument, re.I)
            else None
        )
        out["cerp"] = bool(re.search(r"Contingent Emergency Response Project", instrument, re.I))
        ipf = pick(vals, r"IPF")
        out["has_ipf_component"] = {"yes": True, "no": False}.get((ipf or "").lower())
        es = pick(vals, r"Environmental and Social")
        out["es_risk_rating"] = RATING_RE.search(es)[1] if es and RATING_RE.search(es) else None
    for field, found in (("name", name_hi is not None), ("country", name_hi is not None),
                         ("project_id_datasheet", hi is not None), ("instrument", hi is not None),
                         ("es_risk_rating", "Environmental and Social" in id_headers)):
        if not out.get(field):
            note(missing, field, found)


def parse_modalities(rows, out, missing):
    start = find(rows, r"Implementation Modalities")
    end = find(rows, r"Approval Date", start or 0)
    if start is None:
        note(missing, "modalities", False)
        return
    found = {}
    for row in rows[start : end if end is not None else start + 12]:
        for c in cells(row):
            for m in CHECKBOX_RE.finditer(c):
                label = clean(m[2])
                key = next((k for k, rx in MODALITIES.items() if re.search(rx, label, re.I)), fold(label).replace(" ", "_"))
                found[key] = bool(m[1])
    out["modalities"] = found
    out["modalities_ticked"] = sorted(k for k, v in found.items() if v)


def parse_dates(rows, out, missing):
    hi = find(rows, r"Approval Date")
    if hi is None:
        note(missing, "approval_date", False)
        note(missing, "closing_date", False)
        return
    vals, _ = grid(rows, hi, lambda r: any(DATE_RE.search(c) for c in r))
    out["approval_date"] = iso_date(pick(vals, r"Approval"))
    out["closing_date"] = iso_date(pick(vals, r"^Expected Closing"))
    program_closing = pick(vals, r"Program Closing")
    if program_closing:
        out["program_closing_date"] = iso_date(program_closing)
    for field in ("approval_date", "closing_date"):
        if not out.get(field):
            note(missing, field, True)


def block_text(rows, pattern) -> str | None:
    i = find(rows, pattern)
    if i is None:
        return None
    parts = []
    for row in rows[i + 1 :]:
        row = cells(row)
        if not row:
            continue
        if STOP_RE.match(row[0]):
            break
        parts.append(" ".join(row))
    return clean(" ".join(parts)) or None


def parse_parties(rows, aliases, missing) -> list[dict]:
    agencies = []
    for i, row in enumerate(rows):
        row = cells(row)
        if not row:
            continue
        if re.match(r"^Borrower:?$", row[0], re.I) and len(row) > 1:
            agencies.append({"role": "borrower", "name_raw": row[1], "name": aliases(row[1])})
        elif re.match(r"^Implementing Agency", row[0], re.I):
            value = next((c for c in row if not re.match(r"^Implementing Agency", c, re.I)), None)
            if not value:
                continue
            # A lone following row continues a wrapped name, e.g. "... Ministry of Local" / "Governments (MINALOC)".
            nxt = cells(rows[i + 1]) if i + 1 < len(rows) else []
            if len(nxt) == 1 and not value.rstrip().endswith(")") and not STOP_RE.match(nxt[0]) and not nxt[0].endswith(":"):
                value = f"{value} {nxt[0]}"
            for name in re.split(r"(?<=\)),\s*(?=[A-Z])", clean(value)):
                if not any(a["role"] == "implementing_agency" and a["name_raw"] == name for a in agencies):
                    agencies.append({"role": "implementing_agency", "name_raw": name, "name": aliases(name)})
    if not any(a["role"] == "borrower" for a in agencies):
        note(missing, "borrower", find(rows, r"^Borrower") is not None)
    if not any(a["role"] == "implementing_agency" for a in agencies):
        note(missing, "implementing_agencies", find(rows, r"^Implementing Agency") is not None)
    return agencies


def label_amount_pairs(rows) -> list[tuple[str, float]]:
    """(label, US$ millions) pairs; handles 'label ‖ amount' rows and label/amount on consecutive rows."""
    pairs = []
    i = 0
    while i < len(rows):
        row = cells(rows[i])
        if len(row) >= 2 and amount(row[-1]) is not None and amount(row[0]) is None:
            pairs.append((row[0], amount(row[-1])))
        elif len(row) == 1 and amount(row[0]) is None and i + 1 < len(rows):
            nxt = cells(rows[i + 1])
            if len(nxt) == 1 and amount(nxt[0]) is not None:
                pairs.append((row[0], amount(nxt[0])))
                i += 1
            else:
                pairs.append((row[0], None))
        elif row:
            pairs.append((row[0], None))
        i += 1
    return pairs


def parse_totals(rows, out, missing):
    totals = {}
    for label, value in label_amount_pairs(rows):
        for key, rx in TOTAL_LABELS.items():
            if key not in totals and value is not None and re.search(rx, label, re.I):
                totals[key] = value
    out["financing_totals_usd_millions"] = totals
    if "total_financing" not in totals:
        note(missing, "total_financing", find(rows, TOTAL_LABELS["total_financing"]) is not None)


def parse_financing(rows, borrower, aliases, flags, missing) -> list[dict]:
    start = find(rows, r"^Financing \(US\$, Millions\)$|^DETAILS")
    if start is None:
        note(missing, "financing", False)
        return []
    end = find(rows, r"^(IDA Resources|Expected Disbursements|INSTITUTIONAL DATA|PRACTICE AREA)", start + 1)
    groups: list[dict] = []
    wbg = None
    for label, value in label_amount_pairs(rows[start + 1 : end]):
        if FINANCING_SECTION_RE.match(label):
            wbg = label.lower().startswith("world")
            continue
        if value is None:
            continue
        if FINANCING_GROUP_RE.match(label) or not groups:
            groups.append({"label": label, "amount": value, "wbg": wbg, "items": []})
        else:
            groups[-1]["items"].append((label, value))

    financing = []
    for g in groups:
        parts = g["items"] or [(g["label"], g["amount"])]
        if g["items"] and abs(sum(v for _, v in g["items"]) - g["amount"]) > AMOUNT_TOLERANCE:
            flags.append({"kind": "financing_group_sum", "field": "financing",
                          "detail": f"{g['label']} = {g['amount']} but its parts sum to {sum(v for _, v in g['items']):.2f}"})
        group_org = aliases(g["label"]) if aliases.known(g["label"]) else None
        for label, value in parts:
            if group_org:  # IDA/IBRD: parts are instruments of the same lender
                org, detail = group_org, label if label != g["label"] else None
            elif COUNTERPART_BORROWER_RE.match(label):
                org, detail = aliases(borrower) if borrower else None, label
            elif re.match(r"^Trust Funds", g["label"], re.I):
                org, detail = (aliases(label) if aliases.known(label) else None), label
            else:
                org, detail = None, label
            financing.append({
                "category": "World Bank Group" if g["wbg"] else "Non-World Bank Group" if g["wbg"] is False else None,
                "group": g["label"],
                "label_raw": label,
                "source": org,
                "detail": detail,
                "amount_usd": round(value * 1_000_000),
                "amount_usd_millions": value,
                "original": {"currency": "USD", "amount_millions": value, "from": "datasheet"},
            })
    return financing


def attach_cover_amounts(financing, cover_amounts, aliases, flags):
    """Cover amounts carry the original currency (e.g. EUR 350M = US$411M); match them to financing rows by USD value."""
    used = set()
    for ca in cover_amounts:
        usd = ca["usd_equivalent_millions"]
        match = next(
            (i for i, f in enumerate(financing)
             if i not in used and usd is not None and abs(f["amount_usd_millions"] - usd) <= AMOUNT_TOLERANCE * max(1, usd)),
            None,
        )
        if match is None:
            flags.append({"kind": "cover_amount_unmatched", "field": "financing",
                          "detail": f"cover {ca['kind']} {ca['currency']} {ca['amount_millions']}M (US${usd}M) matches no datasheet row"})
            continue
        used.add(match)
        f = financing[match]
        f["original"] = {"currency": ca["currency"], "amount_millions": ca["amount_millions"], "from": "cover"}
        f["cover_kind"] = ca["kind"]
        if ca["from"]:
            f["cover_from"] = ca["from"]
            if f["source"] is None or not aliases.known(f["label_raw"]) and aliases.known(ca["from"]):
                f["source"] = aliases(ca["from"])


def parse_risks(rows, out, missing) -> list[dict]:
    start = find(rows, r"SYSTEMATIC OPERATIONS RISK")
    if start is None:
        note(missing, "risks", False)
        return []
    end = find(rows, r"COMPLIANCE", start)
    risks = []
    for row in rows[start : end if end is not None else len(rows)]:
        row = cells(row)
        if len(row) < 2:
            continue
        rating = RATING_RE.search(" ".join(row[1:]))
        m = re.match(r"^(\d+)\.\s*(.+)$", row[0])
        if m and rating:
            risks.append({"category": clean(m[2]), "rating": rating[1], "scope": "operation"})
        elif rating and re.search(r"Overall MPA", row[0], re.I):
            risks.append({"category": "Overall MPA Program", "rating": rating[1], "scope": "mpa_program"})
    out["overall_risk"] = next((r["rating"] for r in risks if r["category"].lower() == "overall"), None)
    if not risks:
        note(missing, "risks", True)
    elif not out["overall_risk"]:
        note(missing, "overall_risk", any(re.search(r"Overall", r["category"], re.I) for r in risks))
    return risks


def parse_practice_areas(rows, out, missing):
    hi = find(rows, r"Practice Area \(Lead\)|^Contributing Practice Areas$")
    if hi is None:
        note(missing, "practice_area_lead", False)
        return
    vals, vi = grid(rows, hi, lambda r: bool(cells(r)))
    lead = pick(vals, r"Lead")
    contributing = pick(vals, r"Contributing") or ""
    # Contributing areas wrap onto a following row in the same column.
    if vi is not None and vi + 1 < len(rows):
        nxt = rows[vi + 1]
        col = next((j for j, c in enumerate(rows[hi]) if re.search("Contributing", c or "", re.I)), None)
        if col is not None and cells(nxt) and all(j >= col - HEADER_SLACK for j, c in enumerate(nxt) if c) and contributing.endswith(";"):
            contributing += " " + " ".join(cells(nxt))
    out["practice_area_lead"] = lead
    out["practice_areas_contributing"] = [clean(p) for p in contributing.split(";") if clean(p)]
    if not lead:
        note(missing, "practice_area_lead", any(re.search("Lead", c or "") for c in rows[hi]))


# --- MPA --------------------------------------------------------------------


PHASE_DATE_RE = re.compile(r"^[A-Z][a-z]{2,3}-\d{2}$")  # "Sept-25", "Jul-26"


def parse_phase_row(row: list[str], project_id: str | None) -> dict | None:
    """One row of the MPA framework table ("Table 1. Proposed MPA Framework"):
    Phase # | Operation ID ("Tanzania; P508698" or a label without an ID) | Sequential/Simultaneous | PDO |
    IPF or PforR | IBRD | IDA | Other (US$M) | Estimated approval | E&S rating.
    Columns shift between pages, so fields are found by content, not position."""
    if not row or not re.fullmatch(r"\d{1,2}", (row[0] or "").strip()):
        return None
    cells = [clean(c) for c in row]
    label_cell = next((c for c in cells[1:] if c), "")
    pid = re.search(r"P\d{6}", label_cell)
    instr_i = next((i for i, c in enumerate(cells) if c in ("IPF", "P4R", "PforR", "DPF")), None)
    amounts = [amount(c) for c in cells[instr_i + 1:]] if instr_i is not None else []
    amounts = [a for a in amounts if a is not None][:3]
    return {
        "phase": int(cells[0]),
        "project_id": pid[0] if pid else None,
        "label": clean(re.sub(r";?\s*P\d{6}", "", label_cell)).rstrip(";, "),
        "sequencing": next((c for c in cells if c in ("Simultaneous", "Sequential")), None),
        "instrument": {"P4R": "PforR"}.get(cells[instr_i], cells[instr_i]) if instr_i is not None else None,
        "ibrd_usd_millions": amounts[0] if len(amounts) > 0 else None,
        "ida_usd_millions": amounts[1] if len(amounts) > 1 else None,
        "other_usd_millions": amounts[2] if len(amounts) > 2 else None,
        "estimated_approval": next((c for c in cells if PHASE_DATE_RE.match(c)), None),
        "es_risk_rating": next((c for c in reversed(cells) if RATING_RE.fullmatch(c)), None),
        "is_this_operation": bool(pid and pid[0] == project_id),
    }


def parse_mpa(doc, sections, rows, project_id, out, flags=None):
    pairs = label_amount_pairs(rows)
    envelope = next((v for label, v in pairs if re.search(r"^MPA Program Financing Envelope", label, re.I) and v), None)
    bank = {k: next((v for label, v in pairs if re.search(rx, label, re.I) and v is not None), None)
            for k, rx in (("ibrd", r"of which Bank Financing \(IBRD\)"), ("ida", r"of which Bank Financing \(IDA\)"),
                          ("other", r"of which Other Financing"))}
    # Every row of the framework table is a phase, with or without a project ID. (An earlier version kept
    # only rows with an ID, so 6 of Tanzania's 15 phases; see docs/lessons.md.)
    phases: dict[int, dict] = {}
    for s in sections:
        if s.subtype != "mpa_framework":
            continue
        for t in s.tables:
            for row in t.rows:
                ph = parse_phase_row(row, project_id)
                if ph and ph["phase"] not in phases:
                    phases[ph["phase"]] = ph
    phases_list = [phases[n] for n in sorted(phases)]
    # Cross-check the table against the datasheet's MPA financing details.
    if flags is not None and phases_list:
        missing = [n for n in range(1, max(phases) + 1) if n not in phases]
        if missing:
            flags.append({"kind": "mpa_phases_missing", "field": "mpa.phases", "detail": f"phase numbers missing from the table: {missing}"})
        for key in ("ibrd", "ida", "other"):
            total = round(sum(p[f"{key}_usd_millions"] or 0 for p in phases_list), 2)
            if bank[key] is not None and abs(total - bank[key]) > AMOUNT_TOLERANCE:
                flags.append({"kind": "mpa_amount_mismatch", "field": f"mpa.{key}",
                              "detail": f"phase table {key.upper()} sums to {total} but the datasheet says {bank[key]}"})
    # The MPA program's own ID appears in running page headers: "<program name> (P######)".
    headers = Counter()
    for page in doc:
        for line in page.get_text().splitlines()[:6]:
            m = re.match(r"^(.{15,140}?)\s*\((P\d{6})\s*\)\s*$", line.strip())
            if m and m[2] != project_id:
                headers[(clean(m[1]), m[2])] += 1
    program = next(((name, pid) for (name, pid), n in headers.most_common() if n >= 2), None)
    out["mpa"] = {
        "program_id": program[1] if program else None,
        "program_name": program[0] if program else None,
        "program_id_source": "running page header" if program else None,
        "program_pdo_text": block_text(rows, r"^MPA Program Development Objective"),
        "financing_envelope_usd_millions": envelope,
        "financing_envelope_breakdown_usd_millions": bank,
        "phase_count": len(phases_list),
        "phases": phases_list,
        "this_phase": next((p["phase"] for p in phases_list if p["is_this_operation"]), None),
    }


# --- countries, derived fields -------------------------------------------------


class Countries:
    def __init__(self, path: Path):
        self.entries = json.loads(path.read_text())["countries"]
        self.by_name = {}
        self.by_code = {}
        for name, e in self.entries.items():
            for variant in [name, *e.get("aliases", [])]:
                self.by_name[fold(variant)] = name
            for code in [e["iso2"], *e.get("api_codes", [])]:
                self.by_code[code.upper()] = name

    def canonical(self, name: str | None = None, code: str | None = None) -> str | None:
        if code and code.upper() in self.by_code:
            return self.by_code[code.upper()]
        return self.by_name.get(fold(name)) if name else None

    def region(self, canonical: str | None) -> str | None:
        return self.entries[canonical]["region"] if canonical in self.entries else None

    def iso2(self, canonical: str | None) -> str | None:
        return self.entries[canonical]["iso2"] if canonical in self.entries else None


def derive_status(approval: str | None, closing: str | None, as_of: str) -> str | None:
    """Pipeline before approval, Closed after closing, Active in between (dates are the PDF's expected dates)."""
    if not approval:
        return None
    if approval > as_of:
        return "Pipeline"
    if closing and closing < as_of:
        return "Closed"
    return "Active"


# --- Projects API -----------------------------------------------------------


def fetch_project(project_id: str, version: str) -> tuple[dict | None, str]:
    """Return (cached response, how). Only successful responses are cached; staff fields are dropped."""
    folder = CACHE_DIR / version
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{project_id}.json"
    if path.exists():
        return json.loads(path.read_text()), "cache"
    url = API_URLS[version].format(id=project_id)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kg-pipeline/0.1"})
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return None, f"error: {e}"
    for p in (data.get("projects") or {}).values():
        for k in [k for k in p if PERSONAL_API_FIELDS.match(k) or "email" in k.lower()]:
            del p[k]
    cached = {"url": url, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "response": data}
    path.write_text(json.dumps(cached, ensure_ascii=False, indent=1))
    return cached, "fetched"


def present(v) -> bool:
    return v not in (None, "", [], {}, "0", 0, "0.0", "N/A")


def api_record(project_id: str, version: str) -> tuple[dict | None, dict]:
    cached, how = fetch_project(project_id, version)
    if cached is None:
        return None, {"version": version, "status": "error", "detail": how}
    projects = cached["response"].get("projects") or {}
    p = projects.get(project_id)
    meta = {"version": version, "url": cached["url"], "fetched_at": cached["fetched_at"]}
    return p, {**meta, "status": "found" if p else "not_found"}


def money(p: dict, *keys) -> float | None:
    for key in keys:
        v = p.get(key)
        if present(v):
            return float(str(v).replace(",", ""))
    return None


def api_fields(project_id: str) -> dict:
    v3, meta3 = api_record(project_id, "v3")
    v2, meta2 = api_record(project_id, "v2")
    fields = {}
    if v3:
        codes = v3.get("countrycode") or []
        fields.update({
            "name": v3.get("project_name"),
            "country": v3.get("countryshortname"),
            "country_code": codes[0] if codes else None,
            "region": v3.get("regionname"),
            "status": v3.get("status"),
            "approval_date": iso_date(v3.get("boardapprovaldate")),
            "closing_date": iso_date(v3.get("closingdate")),
            "total_commitment_usd": money(v3, "curr_total_commitment", "totalamt"),
            "ida_commitment_usd": money(v3, "curr_ida_commitment", "idacommamt"),
            "ibrd_commitment_usd": money(v3, "curr_ibrd_commitment", "ibrdcommamt"),
            "grant_usd": money(v3, "grantamt"),
            "borrower": v3.get("borrower"),
            # Names contain ", " (e.g. "Ministry of Water, Energy and Mines"); separate agencies are joined by "," alone.
            "implementing_agencies": [clean(a) for a in re.split(r",(?! )", v3.get("impagency") or "") if clean(a)],
            "es_risk_rating": ES_RATING_CODES.get((v3.get("esrc_ovrl_risk_rate") or "").upper()),
            "pdo": v3.get("pdo"),
        })
    if v2:
        # The API truncates project_gp_info mid-tag, so don't require the closing "]]>".
        gp = re.search(r"<GP_PRACTICE_NAME><!\[CDATA\[(.*?)\]\]", v2.get("project_gp_info") or "")
        fallback = {
            "lending_instrument": v2.get("lendinginstr"),
            "practice_area": gp[1] if gp else None,
            "name": v2.get("project_name"),
            "country": v2.get("countryshortname"),
            "region": v2.get("regionname"),
            "status": v2.get("status"),
        }
        for k, v in fallback.items():
            if present(v) and not present(fields.get(k)):
                fields[k] = v
    fields = {k: v for k, v in fields.items() if present(v)}
    status = "ok" if len(fields) >= 6 else "sparse" if fields else "not_found"
    return {"status": status, "sources": [meta3, meta2], "fields": fields}


INSTRUMENT_CODES = {"program-for-results financing": "PforR", "investment project financing": "IPF", "development policy lending": "DPF",
                    "development policy financing": "DPF"}


def conflicts_with_api(op: dict, agencies: list[dict], pdo: str | None, financing: list[dict], api: dict,
                       countries: Countries) -> list[dict]:
    a = api["fields"]
    out = []

    def check(field, pdf_value, api_value, same, note=None):
        if present(pdf_value) and present(api_value) and not same(pdf_value, api_value):
            out.append({"field": field, "pdf": pdf_value, "api": api_value, **({"note": note} if note else {})})

    check("name", op.get("name"), a.get("name"), lambda x, y: fold(x) == fold(y))
    check("country", op.get("country"), a.get("country"),
          lambda x, y: (countries.canonical(x) or fold(x)) == (countries.canonical(y, a.get("country_code")) or fold(y)))
    check("region", op.get("region_derived"), a.get("region"), lambda x, y: fold(x) == fold(y),
          note="pdf value derived from country via countries.json")
    check("status", op.get("status_derived"), a.get("status"), lambda x, y: fold(x) == fold(y),
          note=f"pdf value derived from the PDF's expected dates as of {op.get('status_derived_as_of')}")
    check("instrument", op.get("instrument"), a.get("lending_instrument"),
          lambda x, y: x == INSTRUMENT_CODES.get(y.lower(), y))
    check("approval_date", op.get("approval_date"), a.get("approval_date"), lambda x, y: x == y,
          note="pdf date is the expected approval date")
    check("closing_date", op.get("closing_date"), a.get("closing_date"), lambda x, y: x == y)
    check("practice_area_lead", op.get("practice_area_lead"), a.get("practice_area"), lambda x, y: fold(x) == fold(y))
    check("es_risk_rating", op.get("es_risk_rating"), a.get("es_risk_rating"), lambda x, y: x == y)
    borrower = next((x["name_raw"] for x in agencies if x["role"] == "borrower"), None)
    check("borrower", borrower, a.get("borrower"), lambda x, y: fold(x) == fold(y))
    pdf_agencies = sorted(x["name_raw"] for x in agencies if x["role"] == "implementing_agency")
    check("implementing_agencies", pdf_agencies, a.get("implementing_agencies"),
          lambda x, y: sorted(map(fold, x)) == sorted(map(fold, y)))
    check("pdo", pdo, a.get("pdo"), lambda x, y: fold(x) == fold(y))
    for lender, key in (("IDA", "ida_commitment_usd"), ("IBRD", "ibrd_commitment_usd")):
        pdf_total = sum(f["amount_usd"] for f in financing if f["source"] and f"({lender})" in f["source"])
        check(f"{lender.lower()}_commitment_usd", pdf_total or None, a.get(key), lambda x, y: abs(x - y) <= 0.5e6)
    return out


# --- main -------------------------------------------------------------------


def extract(pdf: Path, subtypes, group_of, aliases, countries: Countries, as_of: str) -> dict:
    with pymupdf.open(pdf) as doc:
        sections, _ = S.split_sections(doc)
        S.assign_types(sections, subtypes, group_of)
        S.assign_scope(sections)
        doc_type = S.document_type(doc)
        header_id = S.document_project_id(doc)
        cover = parse_cover(doc)
        rows = datasheet_rows(sections)
        op: dict = {}
        flags: list[dict] = []
        missing: list[str] = []
        if not rows:
            note(missing, "datasheet", False)
        parse_basic(rows, op, missing)
        parse_modalities(rows, op, missing)
        parse_dates(rows, op, missing)
        pdo = block_text(rows, r"^Proposed (Program |Project )?Development Objective")
        if not pdo:
            note(missing, "pdo_text", find(rows, r"Development Objective") is not None)
        agencies = parse_parties(rows, aliases, missing)
        borrower = next((a["name_raw"] for a in agencies if a["role"] == "borrower"), None)
        parse_totals(rows, op, missing)
        financing = parse_financing(rows, borrower, aliases, flags, missing)
        attach_cover_amounts(financing, cover["cover_amounts"], aliases, flags)
        risks = parse_risks(rows, op, missing)
        parse_practice_areas(rows, op, missing)
        if op.get("modalities", {}).get("mpa"):
            parse_mpa(doc, sections, rows, op.get("project_id_datasheet") or header_id, op, flags)

    # Project ID: from the page header (regex), confirmed against the datasheet's Operation/Project ID cell.
    pid = op.get("project_id_datasheet") or header_id
    op["project_id"] = pid
    op["project_id_confirmed"] = bool(header_id and op.get("project_id_datasheet") == header_id)
    if not op["project_id_confirmed"]:
        flags.append({"kind": "project_id_unconfirmed", "field": "project_id",
                      "detail": f"page header {header_id!r} vs datasheet {op.get('project_id_datasheet')!r}"})

    total = op.get("financing_totals_usd_millions", {}).get("total_financing")
    listed = round(sum(f["amount_usd_millions"] for f in financing), 2)
    if total is not None and financing and abs(listed - total) > AMOUNT_TOLERANCE:
        flags.append({"kind": "financing_total_mismatch", "field": "financing",
                      "detail": f"sources sum to {listed} but Total Financing is {total}"})

    country = countries.canonical(op.get("country"))
    op["country_canonical"] = country
    op["country_iso2"] = countries.iso2(country)
    if op.get("country") and not country:
        flags.append({"kind": "country_unmapped", "field": "country", "detail": f"{op['country']!r} is not in {COUNTRIES_PATH}"})
    op["region_derived"] = countries.region(country)
    op["status_derived"] = derive_status(op.get("approval_date"), op.get("closing_date"), as_of)
    op["status_derived_as_of"] = as_of

    api = api_fields(pid) if pid else {"status": "no_project_id", "sources": [], "fields": {}}
    if api["status"] != "ok":
        flags.append({"kind": f"api_{api['status']}", "field": "api",
                      "detail": "; ".join(f"{m['version']}: {m['status']}" for m in api["sources"])})
    for key, value in api["fields"].items():
        op[f"{key}_api"] = value
    for field in ("region", "status"):
        api_value = api["fields"].get(field)
        op[field] = api_value or op[f"{field}_derived"]
        op[f"{field}_source"] = "api" if api_value else "derived" if op[f"{field}_derived"] else None
    conflicts = conflicts_with_api(op, agencies, pdo, financing, api, countries)

    for field, status in missing:
        detail = "label found in datasheet but value not parsed" if status == "unparsed" else "datasheet has no such field"
        flags.append({"kind": status, "field": field, "detail": detail})
    if not cover["report_number"]:
        blank = cover["report_label_found"]
        flags.append({"kind": "blank" if blank else "absent", "field": "report_number",
                      "detail": "cover has 'Report No:' with no number" if blank else "no report number on cover"})
    if not cover["date"]:
        flags.append({"kind": "unparsed", "field": "document_date", "detail": "no date found on cover"})

    return {
        "pdf": pdf.name,
        "operation": op,
        "document": {"doc_type": doc_type, "report_number": cover["report_number"], "date": cover["date"],
                     "cover_amounts": cover["cover_amounts"]},
        "financing": financing,
        "agencies": agencies,
        "risks": risks,
        "pdo_text": pdo,
        "api": {"status": api["status"], "sources": api["sources"]},
        "flags": flags,
        "conflicts": conflicts,
    }


def summary_row(d: dict) -> list[str]:
    op = d["operation"]
    wbg = sum(f["amount_usd_millions"] for f in d["financing"] if f["category"] == "World Bank Group")
    unparsed = [f["field"] for f in d["flags"] if f["kind"] in ("unparsed", "blank")]
    absent = [f["field"] for f in d["flags"] if f["kind"] == "absent"]
    return [
        d["pdf"][:14],
        (op.get("project_id") or "?") + ("" if op.get("project_id_confirmed") else " (!)"),
        d["document"]["doc_type"].replace("_document", "").replace("_", " "),
        d["document"]["report_number"] or "-",
        d["document"]["date"] or "-",
        (op.get("instrument") or "?") + (" CERP" if op.get("cerp") else ""),
        op.get("country") or "?",
        f"{op.get('region') or '?'} ({(op.get('region_source') or '?')[0]})",
        f"{op.get('status') or '?'} ({(op.get('status_source') or '?')[0]})",
        op.get("approval_date") or "?",
        op.get("closing_date") or "?",
        f"{wbg:,.2f}",
        f"{op.get('financing_totals_usd_millions', {}).get('total_financing', 0):,.2f}",
        str(len(d["financing"])),
        str(len([a for a in d["agencies"] if a["role"] == "implementing_agency"])),
        op.get("overall_risk") or "?",
        op.get("es_risk_rating") or "?",
        ",".join(op.get("modalities_ticked", [])) or "-",
        d["api"]["status"],
        str(len(d["conflicts"])),
        str(len(unparsed)),
        str(len(absent)),
    ]


def print_summary(docs: list[dict]) -> None:
    headers = ["pdf", "project", "doc type", "report no", "doc date", "instr", "country", "region (a=api/d=derived)", "status", "approval", "closing",
               "WBG US$m", "total US$m", "fin", "IAs", "risk", "E&S", "modalities", "api", "confl", "unparsed", "absent"]
    rows = [summary_row(d) for d in docs]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)))


def main() -> None:
    pdf_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else paths.PDFS
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else paths.OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    subtypes, group_of = S.load_types(TYPES_PATH)
    aliases = Aliases(ALIASES_PATH)
    countries = Countries(COUNTRIES_PATH)
    as_of = date.today().isoformat()

    docs = []
    for pdf in sorted(pdf_dir.glob("*.pdf")):
        d = extract(pdf, subtypes, group_of, aliases, countries, as_of)
        (out_dir / f"{pdf.stem}.datasheet.json").write_text(json.dumps(d, ensure_ascii=False, indent=1))
        docs.append(d)

    print_summary(docs)
    print(f"\n=== Conflicts, PDF vs Projects API ({sum(len(d['conflicts']) for d in docs)})\n")
    for d in docs:
        for c in d["conflicts"]:
            print(f"  {d['pdf'][:14]}  {c['field']:22} pdf={c['pdf']!r}  api={c['api']!r}" + (f"  ({c['note']})" if c.get("note") else ""))
    missing_kinds = ("unparsed", "blank", "absent")
    print(f"\n=== Datasheet fields not extracted ({sum(1 for d in docs for f in d['flags'] if f['kind'] in missing_kinds)})\n")
    for d in docs:
        for f in d["flags"]:
            if f["kind"] in missing_kinds:
                print(f"  {d['pdf'][:14]}  {f['kind']:9} {f['field']:22} {f['detail']}")
    print("\n=== Other flags\n")
    for d in docs:
        for f in d["flags"]:
            if f["kind"] not in missing_kinds:
                print(f"  {d['pdf'][:14]}  {f['kind']:26} {f['detail']}")
    print(f"\n  -> {out_dir}/*.datasheet.json")


if __name__ == "__main__":
    main()
