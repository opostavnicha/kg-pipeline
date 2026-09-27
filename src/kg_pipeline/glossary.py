"""Parse a document's "ABBREVIATIONS AND ACRONYMS" list into (acronym, expansion) pairs.

The list sits in the front matter, either as a 2-column table or as text. In text it comes as
"ACR Expansion" on one line, or as the acronym and expansion on separate lines, in either order
(two-column layouts are read row by row, so "African Development Bank" can precede "AfDB").
"""

import re

ACRONYM_TOKEN_RE = re.compile(r"^[^\W_][\w&/\-.']{0,19}$")  # \w so non-ASCII capitals (Türkiye's "ÖBA") count
LEADER_RE = re.compile(r"\.{4,}|(?:\. ){3,}")
START_RE = re.compile(r"ABBREVIATIONS\s+AND\s+ACRONYMS|ACRONYMS\s+AND\s+ABBREVIATIONS|ABBREVIATIONS", re.IGNORECASE)
END_RE = re.compile(r"^TABLE OF CONTENTS|^CONTENTS$", re.IGNORECASE)


def is_acronym_token(tok: str) -> bool:
    """'AfDB', 'PforR', 'AIP-Water', 'COVID-19', 'CALM2', 'ÖBA' yes; 'Gender-Based', 'Community-Led', 'Turkiye' no:
    at least two capitals, and not a hyphenated compound of capitalized words."""
    if not ACRONYM_TOKEN_RE.match(tok) or not (tok[0].isupper() or tok[0].isdigit()):
        return False
    if sum(c.isupper() for c in tok) < 2:
        return False
    parts = tok.split("-")
    return not (len(parts) > 1 and all(p[:1].isupper() and p[1:].islower() and p[1:].isalpha() for p in parts))


def split_line(line: str) -> tuple[str | None, str | None]:
    """'IDA International Development Association' -> ('IDA', 'International ...');
    'AfDB' -> ('AfDB', None); 'African Development Bank' -> (None, 'African Development Bank')."""
    toks = line.split()
    n = 0
    while n < len(toks) and is_acronym_token(toks[n]):
        n += 1
    if n == 0:
        return None, line
    if n == len(toks):
        # All tokens look like acronyms: one acronym ("AfDB", "AFE WASH") with no expansion on this line.
        return (line, None) if n <= 2 else (None, line)
    return " ".join(toks[:n]), " ".join(toks[n:])


def plausible(acronym: str, expansion: str) -> bool:
    return 1 < len(acronym) <= 25 and len(expansion) >= 3 and not LEADER_RE.search(expansion) and acronym != expansion


def from_text(text: str) -> list[tuple[str, str]]:
    lines = [l.strip() for l in text.splitlines()]
    start = next((i for i, l in enumerate(lines) if START_RE.search(l)), None)
    if start is None:
        return []
    pairs = []
    pending_acr = pending_exp = None
    for line in lines[start + 1:]:
        if not line:
            continue
        if END_RE.search(line) or LEADER_RE.search(line):
            break
        acr, exp = split_line(line)
        if acr and exp:
            pairs.append((acr, exp))
            pending_acr = pending_exp = None
        elif acr:
            if pending_exp:
                pairs.append((acr, pending_exp))
                pending_exp = None
            else:
                pending_acr = acr
        elif exp:
            if pending_acr:
                pairs.append((pending_acr, exp))
                pending_acr = None
            else:
                pending_exp = exp
    return pairs


def from_tables(tables: list[dict]) -> list[tuple[str, str]]:
    pairs = []
    for t in tables:
        for row in t["rows"]:
            cells = [c.strip() for c in row if c and c.strip()]
            # Two-column tables are reliable: any short first cell is the acronym (e.g. "Rwf", "e-GP").
            if len(cells) == 2 and len(cells[0]) <= 15 and len(cells[0].split()) <= 2 and len(cells[1]) > len(cells[0]):
                pairs.append((cells[0], cells[1]))
    return pairs


def parse(front_matter: dict) -> list[tuple[str, str]]:
    """(acronym, expansion) pairs from a front-matter section dict (text + tables), first definition wins."""
    seen = {}
    for acr, exp in from_tables(front_matter.get("tables", [])) + from_text(front_matter.get("text", "")):
        acr, exp = re.sub(r"\s+", " ", acr).strip(), re.sub(r"\s+", " ", exp).strip()
        if plausible(acr, exp) and acr not in seen:
            seen[acr] = exp
    return list(seen.items())
