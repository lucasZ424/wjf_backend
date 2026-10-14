"""D48: the specification index has no broken link (方案 D48, 终版 §18).

The README, the design, the plan, the execution record and every document they reach through links are read
through those links; a link to a file or an anchor that is not there sends the reader to nothing.  What the index
does not reach - the audits of the old code, the superseded papers in 历史方案/ - is kept as it was.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ["README.md", "dev-docs/HTSG_V1_终版设计_v1.0.md", "dev-docs/HTSG_V1_开发实施方案_v1.0.md", "dev-docs/HTSG_V1_开发执行记录_v0.1.md"]
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")
ANCHOR = re.compile(r'<a\s+(?:id|name)="([^"]+)"')


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", text))


def anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    found = set(ANCHOR.findall(text))
    in_code = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and line.startswith("#"):
            found.add(slug(line.lstrip("#")))
    return found


def broken(document: Path) -> list[str]:
    problems = []
    for target in LINK.findall(document.read_text(encoding="utf-8")):
        if re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith("mailto:"):
            continue  # an outside address is not ours to check
        name, _, anchor = target.partition("#")
        path = (document.parent / name).resolve() if name else document
        if not path.exists():
            problems.append(f"{target}: no such file")
        elif anchor and path.suffix == ".md" and anchor not in anchors(path):
            problems.append(f"{target}: no such anchor")
    return problems


def index() -> list[Path]:
    """The entry documents and every markdown document reachable from them."""
    seen, queue = set(), [ROOT / name for name in ENTRY]
    while queue:
        document = queue.pop()
        if document in seen:
            continue
        seen.add(document)
        for target in LINK.findall(document.read_text(encoding="utf-8")):
            name = target.partition("#")[0]
            path = (document.parent / name).resolve() if name and not re.match(r"[a-z][a-z0-9+.-]*:", name) else None
            if path is not None and path.suffix == ".md" and path.exists():
                queue.append(path)
    return sorted(seen)


@pytest.mark.parametrize("document", index(), ids=lambda p: p.name)
def test_the_links_of_a_current_document_lead_somewhere(document):
    assert broken(document) == []


def test_the_checker_really_finds_a_missing_file_and_a_missing_anchor(tmp_path):
    (tmp_path / "there.md").write_text('# Heading\n<a id="named"></a>\n', encoding="utf-8")
    page = tmp_path / "page.md"
    page.write_text("[ok](there.md#heading) [ok](there.md#named) [file](gone.md) [anchor](there.md#absent) [web](https://example.org/x)", encoding="utf-8")
    assert broken(page) == ["gone.md: no such file", "there.md#absent: no such anchor"]
