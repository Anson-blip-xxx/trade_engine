"""Catch malformed nesting before the browser silently reparents page content."""

from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "web"
VOID = frozenset(
    [
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    ]
)


class WorkspaceMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.nodes = {}
        self.footers = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        node = (tag, attrs.get("id"), frozenset(attrs.get("class", "").split()))
        if node[1]:
            assert node[1] not in self.nodes, f"duplicate id: {node[1]}"
            self.nodes[node[1]] = (node, tuple(self.stack))
        if tag == "footer":
            self.footers.append(tuple(self.stack))
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1][0] == tag, (
            f"line {self.getpos()[0]}: unexpected </{tag}>; "
            f"open element is {self.stack[-1] if self.stack else None}"
        )
        self.stack.pop()


@pytest.mark.parametrize("page", ["dashboard", "accounts", "strategy"])
def test_explicit_balanced_nesting_and_workspace_containment(page):
    parser = WorkspaceMarkup()
    parser.feed((ROOT / f"v2_{page}" / "index.html").read_text())
    parser.close()
    assert parser.stack == []
    panels = [
        (node, parents)
        for node, parents in parser.nodes.values()
        if "tab-panel" in node[2]
    ]
    assert panels
    for node, parents in panels:
        assert ("main", "workspace") in [(tag, id_) for tag, id_, _ in parents], node
    assert len(parser.footers) == 1
    assert parser.footers[0][-1][:2] == ("main", "workspace")
    if page == "dashboard":
        node, parents = parser.nodes["health-list"]
        assert parents[-1][0] == "article" and "card" in parents[-1][2], node
        assert any(id_ == "panel-health" for _, id_, _ in parents)


def test_stray_div_is_rejected_even_if_browser_would_repair_it():
    with pytest.raises(AssertionError, match="unexpected </div>"):
        WorkspaceMarkup().feed("<main><section><article><div></div></div>")
