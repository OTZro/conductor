from conductor.plugins.src_jira.impl import _adf_to_markdown


def doc(*content):
    return {"type": "doc", "content": list(content)}


def text(value, *marks):
    node = {"type": "text", "text": value}
    if marks:
        node["marks"] = [m if isinstance(m, dict) else {"type": m} for m in marks]
    return node


def para(*content):
    return {"type": "paragraph", "content": list(content)}


def test_headings_and_marks():
    md = _adf_to_markdown(
        doc(
            {"type": "heading", "attrs": {"level": 3}, "content": [text("Severity")]},
            para(text("S3 "), text("entirely", "strong"), text(" broken "), text("api/x", "code")),
        )
    )
    assert md == "### Severity\n\nS3 **entirely** broken `api/x`"


def test_code_mark_never_nests_emphasis():
    md = _adf_to_markdown(doc(para(text("x", "code", "strong"))))
    assert md == "`x`"


def test_emphasis_delimiters_hug_their_text():
    md = _adf_to_markdown(doc(para(text("a "), text(" bold ", "strong"), text(" b"))))
    assert md == "a  **bold**  b"


def test_link_mark():
    md = _adf_to_markdown(
        doc(para(text("see", {"type": "link", "attrs": {"href": "https://x/y"}})))
    )
    assert md == "[see](https://x/y)"


def test_lists_keep_markers_and_nesting():
    nested = {"type": "bulletList", "content": [{"type": "listItem", "content": [para(text("deep"))]}]}
    md = _adf_to_markdown(
        doc(
            {
                "type": "orderedList",
                "attrs": {"order": 2},
                "content": [
                    {"type": "listItem", "content": [para(text("first")), nested]},
                    {"type": "listItem", "content": [para(text("second"))]},
                ],
            }
        )
    )
    assert md == "2. first\n\n   - deep\n3. second"


def test_code_block_keeps_language_and_body():
    md = _adf_to_markdown(
        doc({"type": "codeBlock", "attrs": {"language": "python"}, "content": [text("a = 1\nb = 2")]})
    )
    assert md == "```python\na = 1\nb = 2\n```"


def test_a_headerless_table_keeps_its_first_row_as_data():
    """ADF flags header cells individually; promoting row zero regardless turned the
    first data row of a headerless table into a header. The separator still has to be
    there (it is what makes it a table), so the header row goes out empty."""

    def row(*cells):
        return {
            "type": "tableRow",
            "content": [{"type": "tableCell", "content": [para(text(c))]} for c in cells],
        }

    md = _adf_to_markdown(doc({"type": "table", "content": [row("a", "b"), row("c", "d")]}))
    assert md == "|  |  |\n| --- | --- |\n| a | b |\n| c | d |"


def test_a_header_cell_outside_row_zero_stays_marked():
    """A ROW header has no markdown equivalent — bold keeps the distinction Jira draws."""
    head = {"type": "tableRow", "content": [
        {"type": "tableHeader", "content": [para(text(c))]} for c in ("H", "V")
    ]}
    body = {"type": "tableRow", "content": [
        {"type": "tableHeader", "content": [para(text("Name"))]},
        {"type": "tableCell", "content": [para(text("x"))]},
    ]}
    md = _adf_to_markdown(doc({"type": "table", "content": [head, body]}))
    assert md.splitlines()[-1] == "| **Name** | x |"


def test_table_gets_a_header_separator_and_escapes_pipes():
    def row(kind, *cells):
        return {
            "type": "tableRow",
            "content": [{"type": kind, "content": [para(text(c))]} for c in cells],
        }

    md = _adf_to_markdown(
        doc({"type": "table", "content": [row("tableHeader", "a", "b"), row("tableCell", "1", "x|y")]})
    )
    assert md == "| a | b |\n| --- | --- |\n| 1 | x\\|y |"


def test_paragraph_that_looks_like_a_marker_is_escaped():
    md = _adf_to_markdown(doc(para(text("- 5% off")), para(text("1. not a list"))))
    assert md == "\\- 5% off\n\n1\\. not a list"


def test_quote_prefixes_every_line_including_blanks():
    md = _adf_to_markdown(
        doc({"type": "blockquote", "content": [para(text("one")), para(text("two"))]})
    )
    assert md == "> one\n> \n> two"


def test_non_adf_input_is_empty():
    assert _adf_to_markdown(None) == ""
    assert _adf_to_markdown("plain") == ""


# ── review follow-ups: ADF text is literal, so the markdown it lands in must be too ──


def test_literal_markdown_in_plain_text_is_escaped():
    """ADF puts formatting in MARKS — these characters are ordinary content, and a
    ticket saying `**kwargs` must not come out bold."""
    md = _adf_to_markdown(doc(para(text("use **kwargs and *args, not ~~that~~ or a_b_c"))))
    assert md == "use \\*\\*kwargs and \\*args, not \\~\\~that\\~\\~ or a\\_b\\_c"


def test_literal_backticks_and_brackets_are_escaped():
    md = _adf_to_markdown(doc(para(text("run `ls` then [see](x)"))))
    assert md == "run \\`ls\\` then \\[see\\](x)"


def test_code_mark_content_is_never_escaped():
    md = _adf_to_markdown(doc(para(text("a_b*c", "code"))))
    assert md == "`a_b*c`"


def test_a_paragraph_that_is_a_rule_is_escaped():
    md = _adf_to_markdown(doc(para(text("---")), para(text("- - -"))))
    assert md == "\\---\n\n\\- - -"


def test_marker_escaping_covers_lines_after_a_hard_break():
    """A hardBreak keeps later lines in the SAME paragraph; an unescaped marker there
    would split it into a list downstream."""
    md = _adf_to_markdown(doc(para(text("intro"), {"type": "hardBreak"}, text("- not a list"))))
    assert md.splitlines()[-1] == "\\- not a list"


def test_code_fence_outruns_backticks_in_the_body():
    """A ``` line inside the block would otherwise close it early and the rest of the
    description would re-parse as prose."""
    md = _adf_to_markdown(
        doc({"type": "codeBlock", "content": [text("before\n```\nafter")]})
    )
    assert md == "````\nbefore\n```\nafter\n````"


def test_code_block_language_is_sanitised_to_an_info_string():
    md = _adf_to_markdown(
        doc({"type": "codeBlock", "attrs": {"language": "c++ (gcc)"}, "content": [text("x")]})
    )
    assert md.splitlines()[0] == "```c++gcc"


def test_em_and_strong_together_avoid_the_ambiguous_triple():
    """`***x***` reads as bold-wrapping-literal-asterisks to a parser that scans ** first."""
    md = _adf_to_markdown(doc(para(text("x", "em", "strong"))))
    assert md == "**_x_**"


def test_block_level_card_keeps_its_url():
    """A top-level blockCard carries its URL in attrs and has no child content — the
    generic container fallback would drop it entirely."""
    md = _adf_to_markdown(
        doc({"type": "blockCard", "attrs": {"url": "https://x/y"}}, para(text("after")))
    )
    assert md == "https://x/y\n\nafter"


def test_task_list_uses_glyphs_not_gfm_checkboxes():
    """The renderer has no task-list branch, so `- [x]` would show its brackets."""
    md = _adf_to_markdown(
        doc(
            {
                "type": "taskList",
                "content": [
                    {"type": "taskItem", "attrs": {"state": "DONE"}, "content": [text("done")]},
                    {"type": "taskItem", "attrs": {"state": "TODO"}, "content": [text("todo")]},
                ],
            }
        )
    )
    assert md == "- ☑ done\n- ☐ todo"


def test_legacy_string_description_is_escaped_not_parsed():
    """A non-ADF description is Jira wiki markup and never met the converter's escaping;
    handed over as markdown unescaped, `*args` / `# incident` / `---` would be reformatted."""
    from conductor.plugins.src_jira.impl import _escape_markdown

    assert _escape_markdown("*args and # incident\n---\n- item") == (
        "\\*args and # incident\n\\---\n\\- item"
    )
