# flake8: noqa
# pylint: disable=missing-function-docstring, missing-class-docstring, redefined-outer-name, line-too-long
# pylint: disable=protected-access
"""Unit tests for rich_tree (adapter + pure render + iterators), Rich Messages phase 2 (#85).

No network / no Kurigram objects: the adapter dispatches by ``type(obj).__name__``, so
every Kurigram Rich* class is stubbed by a tiny dynamically-named object carrying the same
FIELD NAMES as the real class (verified against site-packages). This keeps the tests fast
and forward-compatible while exercising the exact dispatch keys production hits.
"""
from types import SimpleNamespace

import pytest

import rich_tree
from sanitizer import sanitize_html


def node(clsname, **fields):
    """A stub instance whose type().__name__ == clsname (the adapter's dispatch key)."""
    obj = type(clsname, (), {})()
    for k, v in fields.items():
        setattr(obj, k, v)
    return obj


def media_obj(fid="fid1", file_size=None, mime_type=None):
    return SimpleNamespace(file_unique_id=fid, file_size=file_size, mime_type=mime_type, file_id="FILEID_" + str(fid))


def para(text):
    return node("RichBlockParagraph", text=text)


def rm(*blocks, is_rtl=False, part=False):
    return SimpleNamespace(blocks=list(blocks), is_rtl=is_rtl, part=part)


@pytest.fixture(autouse=True)
def _reset_counter():
    rich_tree.reset_counters()
    yield
    rich_tree.reset_counters()


# ======================================================================================
# Adapter
# ======================================================================================
class TestAdapterBasics:
    def test_envelope_fields(self):
        tree = rich_tree.from_pyrogram(rm(para("hi"), is_rtl=True, part=True))
        assert tree["v"] == rich_tree.SCHEMA_V
        assert tree["rtl"] is True
        assert tree["part"] is True
        assert tree["blocks"] == [{"t": "paragraph", "text": "hi"}]

    def test_heading_and_pre_and_footer_and_divider_and_math_and_anchor(self):
        blocks = rich_tree.from_pyrogram(rm(
            node("RichBlockSectionHeading", size=2, text="H"),
            node("RichBlockPreformatted", language="python", text="code"),
            node("RichBlockFooter", text="foot"),
            node("RichBlockDivider"),
            node("RichBlockMathematicalExpression", expression="x^2"),
            node("RichBlockAnchor", name="sec1"),
        ))["blocks"]
        assert blocks[0] == {"t": "heading", "size": 2, "text": "H"}
        assert blocks[1] == {"t": "pre", "language": "python", "text": "code"}
        assert blocks[2] == {"t": "footer", "text": "foot"}
        assert blocks[3] == {"t": "divider"}
        assert blocks[4] == {"t": "math", "expr": "x^2"}
        assert blocks[5] == {"t": "anchor", "name": "sec1"}

    def test_unsupported_and_unknown_block(self):
        # Explicit RichBlockUnsupported and an entirely unknown class both degrade.
        blocks = rich_tree.from_pyrogram(rm(
            node("RichBlockUnsupported"),
            node("RichBlockSomeFutureThing", text="x"),
        ))["blocks"]
        assert blocks == [{"t": "unsupported"}, {"t": "unsupported"}]

    def test_broken_block_is_fail_soft_unsupported(self):
        # A block whose adaptation raises (heading.size access explodes) becomes unsupported
        # and bumps the /health counter — it never crashes the post.
        class Boom:
            @property
            def size(self):
                raise ValueError("boom")
            text = "x"
        Boom.__name__ = "RichBlockSectionHeading"  # dispatch key = class name
        boom = Boom()
        tree = rich_tree.from_pyrogram(rm(boom, para("survivor")))
        assert tree["blocks"][0] == {"t": "unsupported"}
        assert tree["blocks"][1] == {"t": "paragraph", "text": "survivor"}
        assert rich_tree.get_rich_block_adapt_failed_count() == 1

    def test_media_without_fid_is_unsupported(self):
        # A photo whose object lacks a non-empty str file_unique_id degrades.
        blocks = rich_tree.from_pyrogram(rm(
            node("RichBlockPhoto", photo=None, caption=None),
            node("RichBlockPhoto", photo=SimpleNamespace(file_unique_id="", file_size=1), caption=None),
            node("RichBlockPhoto", photo=media_obj("good"), caption=None),
        ))["blocks"]
        assert blocks[0] == {"t": "unsupported"}
        assert blocks[1] == {"t": "unsupported"}
        assert blocks[2]["t"] == "photo" and blocks[2]["fid"] == "good"

    def test_media_nodes_carry_size_mime_caption(self):
        cap = node("RichBlockCaption", text="cap", credit=None)
        blocks = rich_tree.from_pyrogram(rm(
            node("RichBlockAudio", audio=media_obj("au", file_size=10, mime_type="audio/mp3"), caption=cap),
            node("RichBlockVideo", video=media_obj("vid", file_size=999), caption=None),
        ))["blocks"]
        assert blocks[0] == {"t": "audio", "fid": "au", "size": 10, "mime": "audio/mp3",
                             "caption": {"text": "cap", "credit": None}}
        assert blocks[1] == {"t": "video", "fid": "vid", "size": 999}


class TestAdapterGuards:
    def test_depth_over_limit_is_placeholder(self):
        # Nest 21 blockquotes; the innermost block sits at depth 21 (> MAX_RICH_DEPTH=20)
        # and becomes a placeholder unsupported node.
        inner = para("deep")
        for _ in range(rich_tree.MAX_RICH_DEPTH + 1):
            inner = node("RichBlockBlockQuotation", blocks=[inner], credit=None)
        tree = rich_tree.from_pyrogram(rm(inner))
        # Walk to the deepest node and assert it degraded.
        cur = tree["blocks"][0]
        depth = 0
        while cur.get("t") == "blockquote" and cur.get("blocks"):
            cur = cur["blocks"][0]
            depth += 1
        assert cur == {"t": "unsupported"}

    def test_node_budget_truncates_including_cells(self):
        # 2001 paragraphs -> exactly MAX_RICH_NODES kept + one truncated node at the end.
        many = [para(str(i)) for i in range(rich_tree.MAX_RICH_NODES + 1)]
        tree = rich_tree.from_pyrogram(rm(*many))
        assert len(tree["blocks"]) == rich_tree.MAX_RICH_NODES + 1
        assert tree["blocks"][-1] == {"t": "truncated"}
        assert all(b.get("t") != "truncated" for b in tree["blocks"][:-1])

    def test_table_cells_count_against_budget(self):
        # A single table with > MAX_RICH_NODES cells truncates (cells count as nodes).
        row = [node("RichBlockTableCell", text=str(i), is_header=False, colspan=1, rowspan=1)
               for i in range(rich_tree.MAX_RICH_NODES + 50)]
        table = node("RichBlockTable", cells=[row], is_bordered=False, is_striped=False, caption=None)
        tree = rich_tree.from_pyrogram(rm(table))
        assert tree["blocks"][-1] == {"t": "truncated"}


class TestLists:
    def _list(self, *items):
        return node("RichBlockList", items=list(items))

    def _item(self, label="•", value=None, itype=None, checkbox=False, checked=False, text="x"):
        return node("RichBlockListItem", label=label, blocks=[para(text)],
                    has_checkbox=checkbox, is_checked=checked, value=value, type=itype)

    def test_unordered_list(self):
        tree = rich_tree.from_pyrogram(rm(self._list(self._item(), self._item())))
        assert tree["blocks"][0]["t"] == "list"
        html = rich_tree.render_html(tree, lambda f: None)
        assert html.startswith("<ul>")
        assert "•" not in html  # label suppressed for ul

    def test_contiguous_decimal_is_ol(self):
        items = [self._item(label="1.", value=1, itype="1"),
                 self._item(label="2.", value=2, itype="1"),
                 self._item(label="3.", value=3, itype="1")]
        html = rich_tree.render_html(rich_tree.from_pyrogram(rm(self._list(*items))), lambda f: None)
        assert "<ol>" in html and "1." not in html  # browser numbers, label suppressed

    def test_start_not_one_falls_back_to_ul_with_label(self):
        items = [self._item(label="5.", value=5, itype="1"),
                 self._item(label="6.", value=6, itype="1")]
        html = rich_tree.render_html(rich_tree.from_pyrogram(rm(self._list(*items))), lambda f: None)
        assert "<ul>" in html and "5." in html and "6." in html

    def test_letter_type_falls_back_to_ul_with_label(self):
        items = [self._item(label="a.", value=1, itype="a"),
                 self._item(label="b.", value=2, itype="a")]
        html = rich_tree.render_html(rich_tree.from_pyrogram(rm(self._list(*items))), lambda f: None)
        assert "<ul>" in html and "a." in html and "b." in html

    def test_gap_in_values_falls_back_to_ul(self):
        items = [self._item(label="1.", value=1, itype="1"),
                 self._item(label="3.", value=3, itype="1")]
        html = rich_tree.render_html(rich_tree.from_pyrogram(rm(self._list(*items))), lambda f: None)
        assert "<ul>" in html and "1." in html and "3." in html

    def test_checkbox_prefix(self):
        items = [self._item(checkbox=True, checked=True), self._item(checkbox=True, checked=False)]
        html = rich_tree.render_html(rich_tree.from_pyrogram(rm(self._list(*items))), lambda f: None)
        assert "☑" in html and "☐" in html

    def test_none_item_skipped(self):
        tree = rich_tree.from_pyrogram(rm(self._list(self._item(text="keep"), None)))
        assert len(tree["blocks"][0]["items"]) == 1

    def test_empty_items_emits_no_block(self):
        tree = rich_tree.from_pyrogram(rm(self._list()))
        assert tree["blocks"] == []


# ======================================================================================
# Render
# ======================================================================================
class TestRender:
    def test_empty_blocks_render_empty_string(self):
        assert rich_tree.render_html({"v": rich_tree.SCHEMA_V, "rtl": False, "part": False, "blocks": []}, lambda f: None) == ""

    def test_wrong_schema_version_is_placard(self):
        out = rich_tree.render_html({"v": 999, "blocks": [{"t": "paragraph", "text": "x"}]}, lambda f: None)
        assert "rich-unsupported" in out and "<p>" not in out

    def test_heading_sizes(self):
        def h(size):
            return rich_tree.render_html(rich_tree.from_pyrogram(rm(node("RichBlockSectionHeading", size=size, text="T"))), lambda f: None)
        assert "<h3>" in h(1) and "<h4>" in h(2) and "<h5>" in h(3)
        assert "<h6>" in h(4) and "<h6>" in h(9)

    def test_table_headers_colspan_clamp_and_caption_trap(self):
        # caption trap: RichBlockTable.caption holds the RichText title.
        cell_h = node("RichBlockTableCell", text="H", is_header=True, colspan=99, rowspan=1)
        cell_d = node("RichBlockTableCell", text="D", is_header=False, colspan=1, rowspan=2)
        table = node("RichBlockTable", cells=[[cell_h], [cell_d]], is_bordered=True, is_striped=False, caption="My Table")
        out = rich_tree.render_html(rich_tree.from_pyrogram(rm(table)), lambda f: None)
        assert "<caption>My Table</caption>" in out
        assert '<th colspan="20">H</th>' in out  # 99 clamped to 20
        assert '<td rowspan="2">D</td>' in out

    def test_details_open_and_summary(self):
        d = node("RichBlockDetails", summary="Sum", blocks=[para("body")], is_open=True)
        out = rich_tree.render_html(rich_tree.from_pyrogram(rm(d)), lambda f: None)
        assert "<details open><summary>Sum</summary>" in out and "<p>body</p>" in out

    def test_blockquote_and_pullquote_credit(self):
        bq = node("RichBlockBlockQuotation", blocks=[para("q")], credit="Author")
        pq = node("RichBlockPullQuotation", text="pull", credit="Src")
        out = rich_tree.render_html(rich_tree.from_pyrogram(rm(bq, pq)), lambda f: None)
        assert "<blockquote><p>q</p><i>Author</i></blockquote>" in out
        assert "<blockquote>pull<i>Src</i></blockquote>" in out

    def test_rt_formatting_wrappers(self):
        p = para(["plain ", node("RichTextBold", text="b"), node("RichTextItalic", text="i"),
                  node("RichTextCode", text="c"), node("RichTextMarked", text="m"),
                  node("RichTextSubscript", text="s"), node("RichTextSuperscript", text="p")])
        out = rich_tree.render_html(rich_tree.from_pyrogram(rm(p)), lambda f: None)
        assert "<b>b</b>" in out and "<i>i</i>" in out and "<code>c</code>" in out
        assert "<mark>m</mark>" in out and "<sub>s</sub>" in out and "<sup>p</sup>" in out

    def test_rt_url_only_when_str(self):
        good = node("RichTextUrl", text="link", url="https://example.com")
        bad = node("RichTextUrl", text="fallback", url=SimpleNamespace())  # non-str -> degrade
        out = rich_tree.render_html(rich_tree.from_pyrogram(rm(para([good, bad]))), lambda f: None)
        assert '<a href="https://example.com">link</a>' in out
        assert "fallback" in out and out.count("<a ") == 1

    def test_rt_text_mention_and_datetime(self):
        tm = node("RichTextTextMention", text="@joe", user=SimpleNamespace(username="joe"))
        from datetime import datetime
        dt = node("RichTextDateTime", text="then", date=datetime(2026, 7, 20, 15, 30))
        out = rich_tree.render_html(rich_tree.from_pyrogram(rm(para([tm, " ", dt]))), lambda f: None)
        assert '<a href="https://t.me/joe">@joe</a>' in out
        assert "2026-07-20 15:30" in out

    def test_rt_reference_and_links_use_rich_prefix(self):
        anchor = node("RichTextAnchor", text="", name="top")
        ref = node("RichTextReference", text="see", name="fn1")
        link = node("RichTextReferenceLink", text="jump", reference_name="fn1")
        out = rich_tree.render_html(rich_tree.from_pyrogram(rm(para([anchor, ref, link]))), lambda f: None)
        assert '<span id="rich-top"></span>' in out
        assert '<span id="rich-fn1">see</span>' in out
        assert '<a href="#rich-fn1">jump</a>' in out

    def test_map_osm_link(self):
        m = node("RichBlockMap", location=SimpleNamespace(latitude=51.5, longitude=-0.12), caption=None)
        out = rich_tree.render_html(rich_tree.from_pyrogram(rm(m)), lambda f: None)
        assert "openstreetmap.org" in out and "51.50000, -0.12000" in out

    def test_media_url_builder_none_is_placeholder(self):
        photo = node("RichBlockPhoto", photo=media_obj("ph"), caption=None)
        out_ok = rich_tree.render_html(rich_tree.from_pyrogram(rm(photo)), lambda f: "http://x/" + f)
        out_none = rich_tree.render_html(rich_tree.from_pyrogram(rm(photo)), lambda f: None)
        assert '<img src="http://x/ph"' in out_ok
        assert "rich-unsupported" in out_none and "<img" not in out_none

    def test_media_kinds_render_expected_elements(self):
        photo = node("RichBlockPhoto", photo=media_obj("p"), caption=None)
        video = node("RichBlockVideo", video=media_obj("v"), caption=None)
        anim = node("RichBlockAnimation", animation=media_obj("a"), caption=None)
        voice = node("RichBlockVoiceNote", voice_note=media_obj("vo"), caption=None)
        out = rich_tree.render_html(rich_tree.from_pyrogram(rm(photo, video, anim, voice)), lambda f: "u/" + f)
        assert "<img src=\"u/p\"" in out
        assert "<video controls src=\"u/v\"" in out
        assert "autoplay loop muted src=\"u/a\"" in out
        assert '<audio controls' in out and 'type="audio/ogg"' in out  # voice default mime


class TestRenderXSSAfterSanitize:
    """Adversarial user strings must be inert AFTER the boundary sanitize pass."""

    def _sanitized(self, *blocks):
        tree = rich_tree.from_pyrogram(rm(*blocks))
        return sanitize_html(rich_tree.render_html(tree, lambda f: "http://x/" + f))

    def test_script_in_text_neutralised(self):
        out = self._sanitized(para("<script>alert(1)</script>"))
        assert "<script" not in out and "alert(1)" in out  # escaped/stripped, not executable

    def test_javascript_href_dropped(self):
        u = node("RichTextUrl", text="x", url="javascript:alert(1)")
        out = self._sanitized(para(u))
        assert "javascript:" not in out

    def test_evil_anchor_name_sanitised(self):
        # A crafted anchor name cannot break out of the id or clobber the DOM: non-[A-Za-z0-9_-]
        # is stripped in the tree, and the sanitizer id-filter keeps only rich-… ids.
        a = node("RichBlockAnchor", name='x"><img src=x onerror=alert(1)>')
        out = self._sanitized(a)
        # No tag breakout and no active handler: the payload's markup chars are stripped in
        # the tree, leaving only inert letters inside the quoted id value.
        assert "<img" not in out
        assert "onerror=" not in out
        assert 'id="rich-ximgsrcxonerroralert1"' in out

    def test_math_and_language_escaped(self):
        m = node("RichBlockMathematicalExpression", expression="<b>x</b> & y")
        out = self._sanitized(m)
        assert "<b>x</b>" not in out  # escaped inside <pre>
        assert "&amp; y" in out or "& y" in out


# ======================================================================================
# Iterators
# ======================================================================================
class TestIterators:
    def test_iter_tree_media_walks_nesting(self):
        # media inside a details block and inside a list item.
        photo = node("RichBlockPhoto", photo=media_obj("ph", file_size=5), caption=None)
        details = node("RichBlockDetails", summary="s", blocks=[photo], is_open=False)
        li_video = node("RichBlockVideo", video=media_obj("vid"), caption=None)
        item = node("RichBlockListItem", label="•", blocks=[li_video], has_checkbox=False,
                    is_checked=False, value=None, type=None)
        lst = node("RichBlockList", items=[item])
        tree = rich_tree.from_pyrogram(rm(details, lst))
        got = list(rich_tree.iter_tree_media(tree))
        assert {"fid": "ph", "size": 5, "kind": "photo"} in got
        assert {"fid": "vid", "size": None, "kind": "video"} in got

    def test_iter_media_objects_walks_live_nesting(self):
        # Live objects (with file_id) inside a collage inside details, plus a list item.
        p1 = node("RichBlockPhoto", photo=media_obj("A"), caption=None)
        collage = node("RichBlockCollage", blocks=[p1], caption=None)
        details = node("RichBlockDetails", summary="s", blocks=[collage], is_open=False)
        v = node("RichBlockVideo", video=media_obj("B"), caption=None)
        item = node("RichBlockListItem", label="•", blocks=[v], has_checkbox=False,
                    is_checked=False, value=None, type=None)
        lst = node("RichBlockList", items=[item])
        live = rm(details, lst)
        fids = sorted(getattr(o, "file_unique_id") for o in rich_tree.iter_media_objects(live))
        assert fids == ["A", "B"]

    def test_iter_media_objects_none_is_empty(self):
        assert list(rich_tree.iter_media_objects(None)) == []
