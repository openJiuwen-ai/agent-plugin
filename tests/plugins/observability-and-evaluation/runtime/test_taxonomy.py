from ojw_evaluation.core.config import TAXONOMIES
from ojw_evaluation.domain import taxonomy as tax


def test_category_counts_derived_from_files():
    """Counts were previously hardcoded (7 and 3) in two scenario files."""
    assert len(tax.load(TAXONOMIES["ours"])) == 7
    assert len(tax.load(TAXONOMIES["mast"])) == 3


def test_categories_numbered_consecutively():
    for name in TAXONOMIES:
        t = tax.load(TAXONOMIES[name])
        assert [c.number for c in t.categories] == list(range(1, len(t) + 1))


def test_slice_returns_only_requested_category():
    t = tax.load(TAXONOMIES["ours"])
    sliced = tax.slice_categories(t, [2])
    assert sliced.startswith("2.")
    assert "\n1." not in sliced
    assert "\n3." not in sliced


def test_slice_multiple_categories():
    t = tax.load(TAXONOMIES["ours"])
    sliced = tax.slice_categories(t, [1, 3])
    assert sliced.startswith("1.")
    assert "\n\n3." in sliced


def test_every_category_has_text_and_name():
    for name in TAXONOMIES:
        for c in tax.load(TAXONOMIES[name]).categories:
            assert c.name.strip()
            assert c.text.strip()


def _leaves(nodes, depth=1):
    for node in nodes:
        if node["children"]:
            yield from _leaves(node["children"], depth + 1)
        else:
            yield node, depth


def test_outline_has_full_hierarchy_with_definitions():
    """The UI explains findings by category, sub-category and failure type."""
    ours = tax.outline(TAXONOMIES["ours"])
    assert [n["code"] for n in ours] == [str(i) for i in range(1, 8)]
    assert ours[0]["name"] == "System Specifications and Control Process Failures"
    assert ours[0]["description"] == "Failures in agentic specifications and processes"
    leaves = list(_leaves(ours))
    assert {depth for _, depth in leaves} == {3}
    assert all(leaf["description"] for leaf, _ in leaves)
    # A code written without its trailing dot is still parsed.
    tool = next(leaf for leaf, _ in leaves if leaf["code"] == "3.1.3")
    assert tool["name"] == "Tool Calling Delay"


def test_outline_mast_has_two_levels():
    mast = tax.outline(TAXONOMIES["mast"])
    assert len(mast) == 3
    leaves = list(_leaves(mast))
    assert {depth for _, depth in leaves} == {2} and len(leaves) == 14
