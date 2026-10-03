"""Tests for the minimal, escape-first markdown renderer."""

import unittest

from locker.md import MAX_README_CHARS, render


class SecurityTests(unittest.TestCase):
    """These are the point of the module: READMEs are untrusted input."""

    def test_script_tag_is_escaped(self):
        out = render("<script>alert(1)</script>")

        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script", out)

    def test_img_onerror_is_dropped(self):
        # Markup-only lines are removed outright, which is strictly safer than
        # showing them: nothing from the line survives into the page.
        out = render("<img src=x onerror=alert(1)>")

        self.assertNotIn("<img", out)
        self.assertNotIn("onerror", out)

    def test_img_onerror_beside_text_is_escaped(self):
        # Escaping still governs any line that carries something to read.
        out = render("<img src=x onerror=alert(1)> caption")

        self.assertNotIn("<img", out)
        self.assertIn("&lt;img", out)
        self.assertIn("caption", out)

    def test_event_handler_attribute_is_escaped(self):
        out = render('<a href="#" onclick="x()">y</a>')

        # The whole tag is escaped, so no anchor markup is produced. The literal
        # text "onclick=" legitimately survives as inert escaped text.
        self.assertNotIn("<a ", out)
        self.assertIn("&lt;a ", out)

    def test_javascript_href_is_not_a_link(self):
        out = render("[x](javascript:alert(1))")

        self.assertNotIn("href=", out)
        self.assertIn("javascript:alert(1)", out)

    def test_data_href_is_not_a_link(self):
        out = render("[x](data:text/html;base64,PHNjcmlwdD4=)")

        self.assertNotIn("href=", out)

    def test_remote_image_renders_as_link(self):
        out = render("![alt](https://example.com/a.png)")

        self.assertNotIn("<img", out)
        self.assertIn('<a href="https://example.com/a.png"', out)

    def test_html_in_fenced_code_is_escaped(self):
        out = render("```\n<script>bad()</script>\n```")

        self.assertIn("<pre><code>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<script", out)

    def test_safe_link_is_rendered(self):
        out = render("[Qwen](https://huggingface.co/Qwen)")

        self.assertIn('<a href="https://huggingface.co/Qwen">Qwen</a>', out)


class ContentNoiseTests(unittest.TestCase):
    """Real model cards carry badge HTML and YAML frontmatter.

    Neither is readable text. Displaying the markup as escaped source is safe
    but produces a wall of tag soup at the top of every card, which is the
    ugliest thing this UI can show.
    """

    def test_markup_only_lines_are_dropped(self):
        out = render(
            '<a href="https://example.com"><img src="https://x/y.svg" /></a>\n'
            "\n# Qwen3-8B\n\nReal prose."
        )

        self.assertNotIn("<a href", out)
        self.assertNotIn("<img", out)
        self.assertNotIn("&lt;a href", out)
        self.assertIn("<h1>Qwen3-8B</h1>", out)
        self.assertIn("Real prose.", out)

    def test_markup_with_text_stays(self):
        out = render('<a href="https://example.com">Read the docs</a>')

        self.assertIn("Read the docs", out)
        self.assertIn("&lt;a ", out)

    def test_markup_inside_a_code_fence_is_kept(self):
        out = render("```\n<a href=\"x\">\n```")

        self.assertIn("<pre><code>", out)
        self.assertIn("&lt;a href=&quot;x&quot;&gt;", out)

    def test_frontmatter_is_stripped(self):
        out = render(
            "---\nlibrary_name: transformers\nlicense: apache-2.0\n---\n\n# Title\n\nBody"
        )

        self.assertNotIn("library_name", out)
        self.assertIn("<h1>Title</h1>", out)
        self.assertIn("Body", out)

    def test_unclosed_frontmatter_is_left_alone(self):
        out = render("---\nlibrary_name: transformers\n\nnot actually frontmatter")

        self.assertIn("library_name", out)

    def test_a_real_horizontal_rule_survives(self):
        out = render("Above\n\n---\n\nBelow")

        self.assertIn("<hr>", out)
        self.assertIn("Above", out)
        self.assertIn("Below", out)

    def test_script_after_frontmatter_is_still_escaped(self):
        out = render("---\nx: y\n---\n<script>alert(1)</script>")

        self.assertNotIn("<script", out)
        self.assertIn("&lt;script&gt;", out)


class RenderTests(unittest.TestCase):
    def test_headings(self):
        for level in range(1, 7):
            with self.subTest(level=level):
                hashes = "#" * level
                out = render(f"{hashes} Title {level}")

                self.assertIn(f"<h{level}>Title {level}</h{level}>", out)

    def test_bold_italic_inline_code(self):
        self.assertIn("<strong>b</strong>", render("**b**"))
        self.assertIn("<em>i</em>", render("*i*"))
        self.assertIn("<code>c</code>", render("`c`"))

    def test_bullet_and_numbered_lists(self):
        self.assertIn("<ul><li>a</li><li>b</li></ul>", render("- a\n- b"))
        self.assertIn("<ol><li>a</li><li>b</li></ol>", render("1. a\n2. b"))

    def test_blockquote_and_hr(self):
        self.assertIn("<blockquote>q</blockquote>", render("> q"))
        self.assertIn("<hr>", render("---"))

    def test_unclosed_fence_does_not_swallow_the_page(self):
        out = render("```\ncode without end")

        self.assertEqual(out.count("<pre>"), out.count("</pre>"))
        self.assertIn("code without end", out)

    def test_unsupported_construct_is_literal_text(self):
        out = render("| a | b |\n| - | - |")

        self.assertNotIn("<table", out)
        self.assertIn("| a | b |", out)

    def test_unicode_passes_through_unmangled(self):
        out = render("日本語 と 🚀 と & 記号")

        self.assertIn("日本語", out)
        self.assertIn("🚀", out)
        self.assertIn("&amp;", out)
        self.assertNotIn("&amp;amp;", out)

    def test_truncates_at_max_chars(self):
        out = render("x" * MAX_README_CHARS + "SENTINEL")

        self.assertNotIn("SENTINEL", out)

    def test_empty_input(self):
        self.assertEqual(render(""), "")


if __name__ == "__main__":
    unittest.main()
