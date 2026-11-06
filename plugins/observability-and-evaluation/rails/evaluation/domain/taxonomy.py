"""Parsing numbered-block taxonomy files.

Generic block parsing: the taxonomy files are flat text where each top-level
category starts with "<n>. ". Category count is derived from the file rather
than hardcoded (the old code had 7 and 3 written into two scenario files).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_TOP_LEVEL = re.compile(r"(?=^\d+\.(?:\s|\n))", re.MULTILINE)
_LEADING_NUM = re.compile(r"^(\d+)\.")


@dataclass(frozen=True)
class Category:
    number: int
    name: str
    text: str


@dataclass(frozen=True)
class Taxonomy:
    name: str
    path: Path
    categories: list[Category]

    def __len__(self) -> int:
        return len(self.categories)


def load(path: str | Path, name: str = "") -> Taxonomy:
    """Parse a taxonomy file into its top-level categories."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"No taxonomy at {path}")
    content = path.read_text(encoding="utf-8").strip()

    categories: list[Category] = []
    for block in (b.strip() for b in _TOP_LEVEL.split(content)):
        if not block:
            continue
        match = _LEADING_NUM.match(block)
        if not match:
            continue
        # The category name is its first two lines: title + one-line description.
        header = " ".join(line.strip() for line in block.split("\n")[:2]).strip()
        categories.append(
            Category(number=int(match.group(1)), name=header, text=block)
        )

    if not categories:
        raise ValueError(f"No top-level categories parsed from {path}")
    return Taxonomy(name=name or path.stem, path=path, categories=categories)


def slice_categories(taxonomy: Taxonomy, numbers: list[int]) -> str:
    """Return the raw text of the requested top-level categories."""
    wanted = set(numbers)
    return "\n\n".join(c.text for c in taxonomy.categories if c.number in wanted)


# One numbered entry: "1.", "1.1.", "1.1.1." (the final dot is sometimes missing).
_ENTRY = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.+)$")


def outline(path: str | Path) -> list[dict]:
    """Parse the full hierarchy for display: categories, sub-categories, failure types.

    Each node is {"code", "name", "description", "children"}. Depth follows the
    code (1 / 1.1 / 1.1.1), so a two-level taxonomy such as MAST yields categories
    whose children are the failure types. Unnumbered lines continue the
    description of the entry above them.
    """
    roots: list[dict] = []
    nodes: dict[str, dict] = {}
    current = None
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _ENTRY.match(line)
        if not match:
            if current is not None:
                current["description"] = f"{current['description']} {line}".strip()
            continue
        code, text = match.group(1), match.group(2).strip()
        name, _, description = text.partition(":")
        current = {"code": code, "name": name.strip().rstrip("."), "description": description.strip(), "children": []}
        nodes[code] = current
        parent = nodes.get(code.rsplit(".", 1)[0]) if "." in code else None
        (parent["children"] if parent else roots).append(current)
    return roots
