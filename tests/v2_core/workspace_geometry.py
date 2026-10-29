"""Reusable real-browser geometry assertions, independent of API/auth fixtures."""


def assert_workspace_geometry(page, kind):
    assert page.locator("main#workspace").count() == 1
    assert page.locator("footer").evaluate(
        "e => e.parentElement.matches('main#workspace')"
    )
    assert page.locator(".tab-panel").evaluate_all(
        "nodes => nodes.every(e => e.closest('main#workspace'))"
    )
    if kind == "dashboard":
        assert (
            page.locator("#panel-health > article.card > #health-list").count() == 1
        ), (
            "health-list escaped its card/tab/main; horizontal-overflow-only checks miss this"
        )
    result = page.locator("main#workspace").evaluate("""main => {
        const box = main.getBoundingClientRect(), css = getComputedStyle(main);
        const left = box.left + parseFloat(css.paddingLeft);
        const right = box.right - parseFloat(css.paddingRight);
        const visible = e => e.getClientRects().length > 0;
        const escaped = [...main.querySelectorAll('.card, .detail-row, footer')].filter(visible).filter(e => {
            const r = e.getBoundingClientRect();
            return r.left < left - 1 || r.right > right + 1;
        }).map(e => e.id || e.className || e.tagName);
        const root = document.documentElement;
        const footer = main.querySelector('footer').getBoundingClientRect();
        const panelBottom = Math.max(...[...main.querySelectorAll('.tab-panel')].filter(visible).map(e => e.getBoundingClientRect().bottom));
        return {escaped, widthOK: root.scrollWidth <= root.clientWidth + 1, footerAfterContent: footer.top >= panelBottom - 1};
    }""")
    assert result["widthOK"], (kind, "document overflow", result)
    assert not result["escaped"], (kind, "escaped main content bounds", result)
    assert result["footerAfterContent"], (kind, "footer overlaps panel", result)


def assert_expanded_page_bottom(page, kind):
    assert_workspace_geometry(page, kind)
    details = page.locator(".tab-panel:not([hidden]) details")
    states = details.evaluate_all("nodes => nodes.map(e => e.open)")
    try:
        details.evaluate_all("nodes => nodes.forEach(e => e.open = true)")
        page.locator("footer").scroll_into_view_if_needed()
        assert_workspace_geometry(page, kind)
        for detail in details.all():
            assert detail.evaluate("e => e.scrollWidth <= e.clientWidth + 1"), (
                kind,
                "expanded details overflow",
            )
    finally:
        details.evaluate_all(
            "(nodes, states) => nodes.forEach((e, i) => e.open = states[i])", states
        )
