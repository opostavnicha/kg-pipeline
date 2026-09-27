"""Shared question format for eval/eval.py (retrieval-only and oracle modes) and eval/run_e2e.py (end to end).

One JSON object per line. Fields (all but id and question optional):
  id                    unique across files (r01.. retrieval set, e01.. end-to-end set)
  question
  category              graph | retrieve | both | edge   (which tools a good answer needs)
  topic                 free-form grouping, e.g. risk, financing, pdo, mpa, lessons, why
  expect_route          tools that should be called, without the mcp__kg__ prefix; an entry that is a list
                        means "any of these", e.g. [["graph_query", "retrieve"]]
  expect_ids            project IDs the answer must mention (retrieval modes count only IDs of operations
                        that have a document in the corpus)
  expect_facts          strings that must appear in the answer; a list entry means "any of these";
                        a string starting with "re:" is a case-sensitive regex (use (?i) for case-insensitive)
  must_not              strings/regexes that indicate a made-up answer
  expect_section_types  section types or subtypes a retrieval hit should come from (retrieval modes only)
  expect_scope          'main' | 'phase' (retrieval modes only)
  note                  where the answer lives, how it was verified
"""

import json
import re
from pathlib import Path

LIST_FIELDS = ("expect_route", "expect_ids", "expect_facts", "must_not", "expect_section_types")
CATEGORIES = {"graph", "retrieve", "both", "edge"}


def load(*files: Path) -> list[dict]:
    items, seen = [], set()
    for f in files:
        for n, line in enumerate(Path(f).read_text().splitlines(), 1):
            if not line.strip():
                continue
            item = json.loads(line)
            where = f"{f}:{n}"
            for key in ("id", "question"):
                if not item.get(key):
                    raise ValueError(f"{where}: missing {key!r}")
            if item["id"] in seen:
                raise ValueError(f"{where}: duplicate id {item['id']!r}")
            if item.get("category") and item["category"] not in CATEGORIES:
                raise ValueError(f"{where}: category must be one of {sorted(CATEGORIES)}")
            seen.add(item["id"])
            for key in LIST_FIELDS:
                item.setdefault(key, [])
            item["_file"] = str(f)
            items.append(item)
    return items


def matches(text: str, pattern: str) -> bool:
    """Plain strings match case-insensitively as substrings; 're:' strings are regexes."""
    if pattern.startswith("re:"):
        return re.search(pattern[3:], text) is not None
    return pattern.lower() in text.lower()


def any_of(text: str, entry: str | list[str]) -> bool:
    return any(matches(text, p) for p in (entry if isinstance(entry, list) else [entry]))


def label(entry: str | list[str]) -> str:
    return " | ".join(entry) if isinstance(entry, list) else entry
