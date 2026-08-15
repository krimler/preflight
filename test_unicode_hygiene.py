#!/usr/bin/env python3
"""
Test suite for unicode_hygiene.py -- the Section 10 matrix.

These tests pin the safety properties, not the convenience features. The ones
that matter most are in FailClosed: every guard must resolve to "original
untouched, nothing written, non-zero exit, specific diagnostic".

Run:  python3 -m unittest -v test_unicode_hygiene
"""

import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

import unicode_hygiene as u

ZWSP = "\u200b"
ZWNJ = "\u200c"
ZWJ = "\u200d"
SHY = "\u00ad"
BOM = "\ufeff"
VS16 = "\ufe0f"


def clean(text, is_tex=True, **kw):
    """Compute the cleaned text and report for a source string."""
    opts = u.Options(**kw)
    return u.process_text(text, "t.tex" if is_tex else "t.txt", is_tex, opts)


def counts_of(report):
    return report["counts"]


class TempTree(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="uhyg-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def write(self, name, data, encoding="utf-8"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data.encode(encoding) if isinstance(data, str) else data)
        return path

    def read(self, path):
        with open(path, "rb") as fh:
            return fh.read()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = u.main(argv)
        return code, out.getvalue(), err.getvalue()


# =============================================================================
# Protected-region correctness -- corruption prevention.
# =============================================================================

class ProtectedRegions(unittest.TestCase):

    def test_verbatim_family_environments(self):
        for env in ("verbatim", "lstlisting", "minted", "alltt", "Verbatim", "comment"):
            src = "a%sb\n\\begin{%s}\nx%sy\n\\end{%s}\nc%sd\n" % (ZWSP, env, ZWSP, env, ZWSP)
            out, rep = clean(src)
            self.assertIn("x%sy" % ZWSP, out, env)
            self.assertNotIn("a%sb" % ZWSP, out, env)
            self.assertEqual(sum(counts_of(rep)["bucket1_stripped"].values()), 2, env)

    def test_inline_verb_all_delimiter_forms(self):
        cases = [
            r"\verb|a%sb|",
            r"\verb!a%sb!",
            r"\verb+a%sb+",
            r"\verb*|a%sb|",
            r"\lstinline[language=C]|a%sb|",
            r"\lstinline|a%sb|",
            r"\mintinline{python}|a%sb|",
        ]
        for tpl in cases:
            src = "before%s " % ZWSP + (tpl % ZWSP) + " after%s\n" % ZWSP
            out, rep = clean(src)
            self.assertIn("a%sb" % ZWSP, out, tpl)
            self.assertEqual(sum(counts_of(rep)["bucket1_stripped"].values()), 2, tpl)

    def test_math_regions(self):
        cases = [
            "$x%sy$",
            r"\(x%sy\)",
            r"\[x%sy\]",
            "$$x%sy$$",
            "\\begin{equation}x%sy\\end{equation}",
            "\\begin{align}x%sy\\end{align}",
            "\\begin{align*}x%sy\\end{align*}",
            "\\begin{pmatrix}x%sy\\end{pmatrix}",
        ]
        for tpl in cases:
            src = "t%s " % ZWSP + (tpl % ZWSP) + "\n"
            out, rep = clean(src)
            self.assertIn("x%sy" % ZWSP, out, tpl)
            self.assertEqual(sum(counts_of(rep)["bucket1_stripped"].values()), 1, tpl)

    def test_escaped_percent_is_not_a_comment(self):
        src = "50\\%% processed%s here\n" % ZWSP
        out, _ = clean(src)
        self.assertNotIn(ZWSP, out)

    def test_invisible_characters_are_stripped_inside_comments(self):
        src = "kept%s\n%% comment%s also cleaned\n" % (ZWSP, ZWSP)
        out, rep = clean(src)
        self.assertEqual(out, "kept\n% comment also cleaned\n")
        self.assertEqual(sum(counts_of(rep)["bucket1_stripped"].values()), 2)

    def test_comments_are_still_lexed_as_comments(self):
        # A comment does not end at a $ or a { -- the region must still be tracked,
        # or a stray '$' in a note would open math for the rest of the document.
        src = "%% note: $5 and { unbalanced and \\begin{verbatim}\n$x%sy$\n" % ZWSP
        out, rep = clean(src)
        self.assertEqual(out, src)          # the $...$ on line 2 is real math
        self.assertEqual(rep["status"], "unchanged")

    def test_folds_never_happen_inside_comments(self):
        # Comments do not render, so a fold there buys nothing and would rewrite
        # commented-out source that may be uncommented later.
        src = "%% draft: \\author{Erd\u0151s} \u2014 see \u201cnote\u201d\u2026\n"
        out, rep = clean(src, fold_punctuation=True, fold_accents=True)
        self.assertEqual(out, src)
        self.assertEqual(counts_of(rep)["fold_punctuation"], {})
        self.assertEqual(counts_of(rep)["fold_accents"], {})
        self.assertTrue(any("comment" in w["detail"] for w in rep["warnings"]))

    def test_comment_stripping_does_not_disturb_neighbouring_code(self):
        src = ("x = 1 %% trailing note%s\n"
               "\\verb|v%sb| %% another%s\n") % (ZWSP, ZWSP, ZWSP)
        out, rep = clean(src)
        self.assertEqual(out, "x = 1 %% trailing note\n\\verb|v%sb| %% another\n" % ZWSP)
        self.assertEqual(sum(counts_of(rep)["bucket1_stripped"].values()), 2)

    def test_percent_eating_a_newline_still_eats_it(self):
        src = "join%s%%%s\nnext\n" % (ZWSP, ZWSP)
        out, _ = clean(src)
        self.assertEqual(out, "join%\nnext\n")

    def test_escaped_dollar_does_not_open_math(self):
        src = "cost \\$5%s and \\$9%s done\n" % (ZWSP, ZWSP)
        out, rep = clean(src)
        self.assertNotIn(ZWSP, out)
        self.assertEqual(sum(counts_of(rep)["bucket1_stripped"].values()), 2)

    def test_escaped_brace_does_not_open_author_context(self):
        # \author\{...\} is not a braced argument, so the accent is NOT locked.
        src = "\\author\\{caf\u00e9\\}\n"
        out, _ = clean(src, fold_accents=True)
        self.assertIn("cafe", out)

    def test_percent_inside_verbatim_is_not_a_comment(self):
        src = "\\begin{verbatim}\n100%% still body%s\n\\end{verbatim}\nafter%s\n" % (ZWSP, ZWSP)
        out, rep = clean(src)
        self.assertIn("100%% still body%s" % ZWSP, out)
        self.assertEqual(sum(counts_of(rep)["bucket1_stripped"].values()), 1)

    def test_end_of_different_env_does_not_close_verbatim(self):
        src = ("\\begin{verbatim}\n\\end{itemize}\n%s\n\\end{verbatim}\nafter%s\n"
               % (ZWSP, ZWSP))
        out, rep = clean(src)
        self.assertIn("\\end{itemize}", out)
        self.assertEqual(sum(counts_of(rep)["bucket1_stripped"].values()), 1)

    def test_nested_math_in_text_in_align_is_conservatively_protected(self):
        src = "\\begin{align}\n a &= b \\text{ note%s $z$ } \\\\\n\\end{align}\nafter%s\n" % (ZWSP, ZWSP)
        out, rep = clean(src)
        self.assertIn("note%s" % ZWSP, out)
        self.assertEqual(sum(counts_of(rep)["bucket1_stripped"].values()), 1)

    def test_backslash_escapes_delimiters_generally(self):
        for esc in (r"\{", r"\}", r"\%", r"\$", r"\&", r"\#", r"\_", r"\~", r"\^"):
            src = "x %s y%s\n" % (esc, ZWSP)
            out, _ = clean(src)
            self.assertNotIn(ZWSP, out, esc)


# =============================================================================
# ZWNJ / ZWJ context gating.
# =============================================================================

class JoinerGating(unittest.TestCase):

    def test_zwj_between_latin_letters_is_stripped(self):
        out, rep = clean("wo%srd\n" % ZWJ)
        self.assertEqual(out, "word\n")
        self.assertEqual(counts_of(rep)["zwnj_zwj_stripped"], 1)

    def test_zwnj_adjacent_to_arabic_preserved(self):
        out, rep = clean("\u0645%s\u06cc\n" % ZWNJ)
        self.assertIn(ZWNJ, out)
        self.assertEqual(counts_of(rep)["zwnj_zwj_preserved"], 1)

    def test_zwnj_adjacent_to_devanagari_preserved(self):
        out, rep = clean("\u0915%s\u0937\n" % ZWNJ)
        self.assertIn(ZWNJ, out)
        self.assertEqual(counts_of(rep)["zwnj_zwj_preserved"], 1)

    def test_zwj_in_emoji_sequence_preserved(self):
        out, rep = clean("\U0001f468%s\U0001f4bb\n" % ZWJ)
        self.assertIn(ZWJ, out)
        self.assertEqual(counts_of(rep)["zwnj_zwj_preserved"], 1)

    def test_zwj_after_base_with_presentation_selector_preserved(self):
        # U+2122 is outside the emoji blocks, but the VS16 makes it pictographic.
        out, rep = clean("\u2122%s%s\U0001f525\n" % (VS16, ZWJ))
        self.assertIn(ZWJ, out)
        self.assertEqual(counts_of(rep)["zwnj_zwj_preserved"], 1)

    def test_adjacency_scan_skips_other_bucket1_chars(self):
        # ZWJ separated from its Arabic neighbour by a soft hyphen is still preserved.
        out, rep = clean("\u0645%s%s\u06cc\n" % (SHY, ZWJ))
        self.assertIn(ZWJ, out)
        self.assertNotIn(SHY, out)

    def test_zwnj_at_protected_boundary_is_preserved(self):
        src = "$x$%sy\n" % ZWNJ
        out, rep = clean(src)
        self.assertIn(ZWNJ, out)
        self.assertEqual(counts_of(rep)["zwnj_zwj_preserved"], 1)


# =============================================================================
# Accent gating (Section 4).
# =============================================================================

class AccentGating(unittest.TestCase):

    def test_body_accent_folds_and_warns(self):
        out, rep = clean("caf\u00e9 au lait\n", fold_accents=True)
        self.assertEqual(out, "cafe au lait\n")
        self.assertEqual(counts_of(rep)["fold_accents"], {"\u00e9->e": 1})
        self.assertTrue(any("accent fold" in w["detail"] for w in rep["warnings"]))

    def test_without_flag_accents_are_never_touched(self):
        src = "caf\u00e9 \u00fc \u00f1 \u00f8 \u00df \u00e6 \u00c5\n"
        out, rep = clean(src)
        self.assertEqual(out, src)
        self.assertEqual(counts_of(rep)["fold_accents"], {})

    def test_fold_table_covers_the_documented_examples(self):
        pairs = [("\u00e9", "e"), ("\u00fc", "u"), ("\u00f1", "n"),
                 ("\u00f8", "o"), ("\u00df", "ss"), ("\u00e6", "ae"), ("\u00c5", "A")]
        for ch, want in pairs:
            self.assertEqual(u.fold_accent(ch), want, ch)

    def test_locked_contexts_are_not_folded(self):
        cases = {
            "author": "\\author{Paul Erd\u0151s}\n",
            "title": "\\title{Sur les \u00e9quations}\n",
            "thanks": "x\\thanks{Merci \u00e0 R\u00e9mi}\n",
            "date": "\\date{f\u00e9vrier}\n",
            "cite": "see \\cite{M\u00fcller2020} here\n",
            "citep-opt": "see \\citep[p.~3]{M\u00fcller2020} here\n",
            "bibliography": "\\bibliography{r\u00e9fs}\n",
            "bibitem": "\\bibitem{k} P. Erd\u0151s, title.\n",
            "thebibliography": ("\\begin{thebibliography}{9}\n"
                                "P. Erd\u0151s\n\\end{thebibliography}\n"),
        }
        for name, src in cases.items():
            out, rep = clean(src, fold_accents=True)
            self.assertEqual(out, src, name)
            self.assertEqual(counts_of(rep)["fold_accents"], {}, name)
            self.assertTrue(any("skipped" in w["detail"] for w in rep["warnings"]), name)

    def test_bucket1_still_stripped_inside_locked_contexts(self):
        # The accent lock gates folds only. Invisible characters still go.
        src = "\\author{Erd\u0151s%s}\n" % ZWSP
        out, rep = clean(src, fold_accents=True)
        self.assertEqual(out, "\\author{Erd\u0151s}\n")
        self.assertEqual(sum(counts_of(rep)["bucket1_stripped"].values()), 1)

    def test_accent_in_math_is_protected_not_merely_locked(self):
        src = "$\\text{caf\u00e9}$\n"
        out, _ = clean(src, fold_accents=True)
        self.assertEqual(out, src)


# =============================================================================
# Fold correctness.
# =============================================================================

class Folds(unittest.TestCase):

    def test_punctuation_map_latex(self):
        cases = [
            ("\u2013", "--"), ("\u2014", "---"), ("\u2015", "---"),
            ("\u2018", "`"), ("\u2019", "'"), ("\u201c", "``"), ("\u201d", "''"),
            ("\u2026", "\\ldots{}"), ("\u00a0", "~"), ("\u2009", "\\,"),
            ("\u2002", " "), ("\u2003", " "), ("\u2007", " "), ("\u2008", " "),
        ]
        for ch, want in cases:
            out, _ = clean("a%sb\n" % ch, fold_punctuation=True)
            self.assertEqual(out, "a%sb\n" % want, ch)

    def test_stray_minus_and_times_become_math_in_text_mode(self):
        out, _ = clean("5 \u2212 3 and 4 \u00d7 5\n", fold_punctuation=True)
        self.assertEqual(out, "5 $-$ 3 and 4 $\\times$ 5\n")

    def test_minus_and_times_inside_math_are_untouched(self):
        src = "$5 \u2212 3$ and $4 \u00d7 5$\n"
        out, rep = clean(src, fold_punctuation=True)
        self.assertEqual(out, src)
        self.assertEqual(counts_of(rep)["fold_punctuation"], {})

    def test_default_run_touches_no_letters_and_no_punctuation(self):
        src = "caf\u00e9 \u2014 \u201cquoted\u201d \u2026 %s here\n" % ZWSP
        out, rep = clean(src)
        self.assertEqual(out, src.replace(ZWSP, ""))
        self.assertEqual(counts_of(rep)["fold_punctuation"], {})
        self.assertEqual(counts_of(rep)["fold_accents"], {})

    def test_all_enables_both_groups(self):
        argv_opts = u._options(u.build_parser().parse_args(["clean", "--all", "x"]))
        self.assertTrue(argv_opts.fold_punctuation)
        self.assertTrue(argv_opts.fold_accents)
        # and disables no guard
        self.assertFalse(argv_opts.force_relex)
        self.assertFalse(argv_opts.force_change_exactness)
        self.assertFalse(argv_opts.force_encoding)
        self.assertFalse(argv_opts.force_lexer)

    def test_character_tables_have_no_silent_key_collisions(self):
        self.assertEqual(len(u.PUNCT_FOLDS_TEX), 18)
        self.assertEqual(len(u.PUNCT_FOLDS_TXT), 18)
        self.assertEqual(set(u.PUNCT_FOLDS_TEX), set(u.PUNCT_FOLDS_TXT))

    def test_bucket1_table_contents(self):
        for cp in (0x200B, 0x2060, 0x2064, 0xFEFF, 0x00AD, 0x034F, 0x180E,
                   0x200E, 0x200F, 0x202A, 0x202E, 0x2066, 0x2069,
                   0xE0000, 0xE007F, 0xFE00, 0xFE0F, 0xE0100, 0xE01EF):
            self.assertIn(cp, u.BUCKET1, hex(cp))
        for cp in (0x200C, 0x200D, 0x0041, 0x00E9, 0x2014):
            self.assertNotIn(cp, u.BUCKET1, hex(cp))


# =============================================================================
# Plain-text-only extras (Section 3.4).
# =============================================================================

class PlainText(unittest.TestCase):

    def test_space_runs_collapse(self):
        out, _ = clean("a    b\n", is_tex=False)
        self.assertEqual(out, "a b\n")

    def test_tabs_normalize(self):
        out, _ = clean("a\t\tb\n", is_tex=False)
        self.assertEqual(out, "a b\n")

    def test_trailing_whitespace_stripped(self):
        out, _ = clean("a b   \nc\t\n", is_tex=False)
        self.assertEqual(out, "a b\nc\n")

    def test_crlf_and_cr_normalized(self):
        out, _ = clean("a\r\nb\rc\n", is_tex=False)
        self.assertEqual(out, "a\nb\nc\n")

    def test_blank_line_runs_collapse_to_two(self):
        out, _ = clean("a\n\n\n\n\n\nb\n", is_tex=False)
        self.assertEqual(out, "a\n\n\nb\n")

    def test_bucket1_between_spaces_still_collapses(self):
        out, _ = clean("a %s b\n" % ZWSP, is_tex=False)
        self.assertEqual(out, "a b\n")

    def test_bom_is_stripped_as_bucket1(self):
        out, rep = clean(BOM + "hello\n", is_tex=False)
        self.assertEqual(out, "hello\n")
        self.assertEqual(counts_of(rep)["bucket1_stripped"], {"U+FEFF": 1})

    def test_whitespace_rules_never_run_on_tex(self):
        src = "a    b   \n\n\n\n\nc\n"
        out, rep = clean(src)
        self.assertEqual(out, src)
        self.assertNotIn("whitespace_normalized", counts_of(rep))

    def test_plaintext_punctuation_folds_are_ascii(self):
        out, _ = clean("a\u2014b \u201cq\u201d \u2026\n", is_tex=False, fold_punctuation=True)
        self.assertEqual(out, 'a--b "q" ...\n')


# =============================================================================
# Fail-closed behaviour -- the core guarantee.
# =============================================================================

class FailClosed(TempTree):

    def assert_refused(self, path, argv, guard=None):
        before = self.read(path)
        code, out, err = self.run_cli(argv)
        self.assertNotEqual(code, 0, "expected non-zero exit")
        self.assertEqual(self.read(path), before, "original bytes changed")
        self.assertFalse(os.path.exists(path + ".bak"), "a .bak was written on refusal")
        leftovers = [f for f in os.listdir(self.dir) if f.startswith(".uhyg-")]
        self.assertEqual(leftovers, [], "temp files left behind")
        text = out + err
        self.assertIn("refused", text)
        if guard:
            self.assertIn(guard, text)
        return text

    def test_unbalanced_begin_verbatim_refuses(self):
        p = self.write("a.tex", "x%s\n\\begin{verbatim}\nbody\n" % ZWSP)
        msg = self.assert_refused(p, ["clean", p], guard="lexer")
        self.assertIn("verbatim", msg)

    def test_unterminated_verb_refuses_with_specific_diagnostic(self):
        p = self.write("a.tex", "x%s \\verb|no closing\nnext line\n" % ZWSP)
        msg = self.assert_refused(p, ["clean", p], guard="lexer")
        self.assertIn("verb", msg)
        self.assertIn("line 1", msg)

    def test_unterminated_math_refuses(self):
        p = self.write("a.tex", "x%s $y + 1\n" % ZWSP)
        self.assert_refused(p, ["clean", p], guard="lexer")

    def test_mismatched_end_refuses(self):
        p = self.write("a.tex", "\\begin{align}x%s\\end{equation}\n" % ZWSP)
        self.assert_refused(p, ["clean", p], guard="lexer")

    def test_bad_verb_delimiter_refuses(self):
        # `*` may not be the delimiter of \verb* (it is consumed as the star).
        p = self.write("a.tex", "x%s \\verb**abc*\n" % ZWSP)
        self.assert_refused(p, ["clean", p], guard="lexer")

    def test_verb_at_end_of_file_refuses(self):
        p = self.write("a.tex", "x%s \\verb" % ZWSP)
        self.assert_refused(p, ["clean", p], guard="lexer")

    def test_dangling_backslash_refuses(self):
        p = self.write("a.tex", "x%s \\" % ZWSP)
        self.assert_refused(p, ["clean", p], guard="lexer")

    def test_change_exactness_catches_an_unlogged_edit(self):
        # Inject a discrepancy: the applier drops a character nobody logged.
        real = u.apply_edits

        def lying(text, edits):
            out, ranges = real(text, edits)
            return out.replace("keepme", "keepm"), ranges

        p = self.write("a.tex", "keepme %s here\n" % ZWSP)
        with mock.patch.object(u, "apply_edits", lying):
            self.assert_refused(p, ["clean", p], guard="change_exactness")

    def test_change_exactness_catches_a_lying_report_row(self):
        real = u.edit_row

        def lying(edit, idx):
            row = real(edit, idx)
            row["line"] = row["line"] + 1000 if row["line"] == 1 else row["line"]
            return row

        p = self.write("a.tex", "abc%sdef\n" % ZWSP)
        with mock.patch.object(u, "edit_row", lying):
            self.assert_refused(p, ["clean", p], guard="change_exactness")

    def test_relex_refuses_when_an_edit_changes_protected_structure(self):
        # A logged edit that deletes a math delimiter: the change report and the
        # output agree, so change-exactness passes and only the re-lex can catch it.
        real = u.compute_edits

        def sabotage(text, lexres, opts, is_tex):
            edits, warnings, extra = real(text, lexres, opts, is_tex)
            k = text.index("$")
            edits.append(u.Edit(k, k + 1, "", "bucket1", "fold", "$", ""))
            edits.sort(key=lambda e: e.start)
            return edits, warnings, extra

        p = self.write("a.tex", "text%s and $x+1$ end\n" % ZWSP)
        with mock.patch.object(u, "compute_edits", sabotage):
            self.assert_refused(p, ["clean", p], guard="relex")

    def test_non_utf8_input_is_refused_not_force_decoded(self):
        p = self.write("a.txt", b"caf\xe9 na\xefve\n")
        self.assert_refused(p, ["clean", p], guard="encoding")

    def test_explicit_encoding_override_accepts_the_file(self):
        # U+00AD (soft hyphen, Bucket 1) is representable in latin-1.
        p = self.write("a.txt", "caf\u00e9 x%sy\n" % SHY, encoding="latin-1")
        code, out, err = self.run_cli(["clean", "--no-backup", "--encoding", "latin-1", p])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.read(p), "caf\u00e9 xy\n".encode("latin-1"))

    def test_force_encoding_is_lossless_latin1(self):
        p = self.write("a.txt", b"caf\xe9 x\xe2\x80\x8b y\n")
        code, out, err = self.run_cli(["clean", "--force-encoding", p])
        self.assertEqual(code, 0)
        self.assertIn("latin-1", out + err)

    def test_symlink_destination_is_refused(self):
        real = self.write("real.tex", "x%s\n" % ZWSP)
        link = os.path.join(self.dir, "link.tex")
        os.symlink(real, link)
        before = self.read(real)
        code, out, err = self.run_cli(["clean", link])
        self.assertNotEqual(code, 0)
        self.assertIn("symlink", out + err)
        self.assertEqual(self.read(real), before)

    def test_unexpected_exception_becomes_a_refusal(self):
        p = self.write("a.tex", "x%s\n" % ZWSP)
        with mock.patch.object(u, "compute_edits", side_effect=KeyError("boom")):
            before = self.read(p)
            code, out, err = self.run_cli(["clean", p])
            self.assertEqual(code, u.EXIT_ERROR)
            self.assertEqual(self.read(p), before)
            self.assertIn("refused", out + err)
            self.assertIn("KeyError", out + err)

    def test_crash_between_temp_write_and_replace_leaves_original_intact(self):
        p = self.write("a.tex", "x%s\n" % ZWSP)
        before = self.read(p)

        def die(tmp):
            raise RuntimeError("simulated kill after fsync, before replace")

        u._after_temp_write_hook = die
        try:
            code, out, err = self.run_cli(["clean", "--no-backup", p])
        finally:
            u._after_temp_write_hook = None
        self.assertNotEqual(code, 0)
        self.assertEqual(self.read(p), before)
        self.assertEqual([f for f in os.listdir(self.dir) if f.startswith(".uhyg-")], [])

    def test_refusal_does_not_abort_the_batch(self):
        good = self.write("good.tex", "a%sb\n" % ZWSP)
        bad = self.write("bad.tex", "\\begin{verbatim}\nunclosed\n")
        code, out, err = self.run_cli(["clean", "--no-backup", good, bad])
        self.assertEqual(code, u.EXIT_REFUSED)
        self.assertEqual(self.read(good), b"ab\n")          # the good file was still cleaned
        self.assertIn("refused", out + err)

    def test_refusal_message_format(self):
        p = self.write("a.tex", "x \\verb|open\n")
        code, out, err = self.run_cli(["clean", p])
        self.assertRegex(out + err, r"refused .*: lexer at line \d+: ")


# =============================================================================
# Escape hatches (Section 7).
# =============================================================================

class ForceFlags(TempTree):

    def test_force_lexer_cleans_the_understood_prefix_only(self):
        p = self.write("a.tex", "good%s\n\\begin{verbatim}\nbad%s\n" % (ZWSP, ZWSP))
        code, out, err = self.run_cli(["clean", "--no-backup", "--force-lexer", p])
        self.assertEqual(code, 0)
        self.assertEqual(self.read(p).decode(), "good\n\\begin{verbatim}\nbad%s\n" % ZWSP)
        self.assertIn("force-lexer", out + err)

    def test_force_relex_lets_a_structural_change_through(self):
        real = u.compute_edits

        def sabotage(text, lexres, opts, is_tex):
            edits, warnings, extra = real(text, lexres, opts, is_tex)
            k = text.index("$")
            edits.append(u.Edit(k, k + 1, "", "bucket1", "fold", "$", ""))
            edits.sort(key=lambda e: e.start)
            return edits, warnings, extra

        p = self.write("a.tex", "t%s $x$ e\n" % ZWSP)
        with mock.patch.object(u, "compute_edits", sabotage):
            code, out, err = self.run_cli(["clean", "--no-backup", "--force-relex",
                                           "--no-compile-verify", p])
        self.assertEqual(code, 0)
        self.assertIn("force-relex", out + err)

    def test_every_force_flag_documents_what_it_waives(self):
        parser = u.build_parser()
        helptext = io.StringIO()
        with redirect_stdout(helptext):
            try:
                parser.parse_args(["clean", "--help"])
            except SystemExit:
                pass
        text = helptext.getvalue()
        for flag in ("--force-relex", "--force-change-exactness", "--force-encoding",
                     "--force-lexer"):
            self.assertIn(flag, text)
        self.assertIn("WAIVES", text)

    def test_no_force_flag_waives_the_write_architecture(self):
        # Even with every force flag set, a symlink destination is still refused.
        real = self.write("real.tex", "x%s\n" % ZWSP)
        link = os.path.join(self.dir, "link.tex")
        os.symlink(real, link)
        code, out, err = self.run_cli([
            "clean", "--force-relex", "--force-change-exactness",
            "--force-encoding", "--force-lexer", link,
        ])
        self.assertNotEqual(code, 0)
        self.assertIn("symlink", out + err)


# =============================================================================
# Compile-verify (Section 6).
# =============================================================================

STANDALONE = (
    "\\documentclass{article}\n"
    "\\usepackage[utf8]{inputenc}\n"
    "\\begin{document}\n"
    "Hello%s world.\n"
    "\\end{document}\n"
)


class CompileVerify(TempTree):

    def setUp(self):
        super().setUp()
        u._compile_cache.clear()

    def _stub_pdflatex(self, script):
        bindir = os.path.join(self.dir, "bin")
        os.makedirs(bindir, exist_ok=True)
        path = os.path.join(bindir, "pdflatex")
        with open(path, "w") as fh:
            fh.write(script)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return bindir

    def test_fragment_is_cleaned_with_no_compile_attempt(self):
        p = self.write("frag.tex", "A fragment%s with no documentclass.\n" % ZWSP)
        code, out, err = self.run_cli(["clean", "--no-backup", "--json", p])
        self.assertEqual(code, 0)
        rep = json.loads(out)
        self.assertEqual(rep["compile_verify"], {"attempted": False, "reason": "fragment"})
        self.assertEqual(self.read(p), b"A fragment with no documentclass.\n")

    def test_is_standalone_heuristic(self):
        self.assertTrue(u.is_standalone("\\documentclass{article}\n"))
        self.assertFalse(u.is_standalone("just text\n"))
        self.assertFalse(u.is_standalone("\\input{pre}\n\\documentclass{article}\n"))
        self.assertTrue(u.is_standalone("% \\input{pre}\n\\documentclass{article}\n"))
        self.assertTrue(u.is_standalone("\\documentclass{a}\n\\includegraphics{f}\n"))

    def test_missing_pdflatex_skips_with_a_warning_and_still_cleans(self):
        p = self.write("doc.tex", STANDALONE % ZWSP)
        with mock.patch.object(u.shutil, "which", return_value=None):
            code, out, err = self.run_cli(["clean", "--no-backup", p])
        self.assertEqual(code, 0)
        self.assertIn("pdflatex not found", out + err)
        self.assertNotIn(ZWSP.encode(), self.read(p))

    def test_edited_into_a_compile_failure_is_refused(self):
        # Stub pdflatex: the original compiles, the cleaned candidate does not.
        bindir = self._stub_pdflatex(
            "#!/bin/sh\n"
            'for a in "$@"; do case "$a" in *uhyg-verify*) exit 1;; esac; done\n'
            "exit 0\n"
        )
        p = self.write("doc.tex", STANDALONE % ZWSP)
        before = self.read(p)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
        with mock.patch.dict(os.environ, env, clear=True):
            code, out, err = self.run_cli(["clean", p])
        self.assertNotEqual(code, 0)
        self.assertIn("compile_verify", out + err)
        self.assertEqual(self.read(p), before)

    def test_original_that_never_compiled_is_not_held_against_the_edit(self):
        bindir = self._stub_pdflatex("#!/bin/sh\nexit 1\n")   # nothing compiles
        p = self.write("doc.tex", STANDALONE % ZWSP)
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
        with mock.patch.dict(os.environ, env, clear=True):
            code, out, err = self.run_cli(["clean", "--no-backup", "--json", p])
        self.assertEqual(code, 0)
        rep = json.loads(out)
        self.assertEqual(rep["compile_verify"],
                         {"attempted": True, "before_ok": False, "after_ok": False})
        self.assertNotIn(ZWSP.encode(), self.read(p))

    def test_no_compile_verify_flag_skips(self):
        p = self.write("doc.tex", STANDALONE % ZWSP)
        code, out, err = self.run_cli(["clean", "--no-backup", "--json",
                                       "--no-compile-verify", p])
        rep = json.loads(out)
        self.assertEqual(rep["compile_verify"], {"attempted": False, "reason": "flag"})

    @unittest.skipUnless(shutil.which("pdflatex"), "pdflatex not installed")
    def test_real_pdflatex_accepts_a_document_that_compiles_before_and_after(self):
        # A soft hyphen is Bucket 1 but is representable under inputenc, so the
        # original compiles too -- the case where both sides must come back OK.
        p = self.write("doc.tex", STANDALONE % SHY)
        code, out, err = self.run_cli(["clean", "--no-backup", "--json", p])
        self.assertEqual(code, 0, out + err)
        rep = json.loads(out)
        self.assertEqual(rep["compile_verify"],
                         {"attempted": True, "before_ok": True, "after_ok": True})
        self.assertNotIn(SHY.encode(), self.read(p))

    @unittest.skipUnless(shutil.which("pdflatex"), "pdflatex not installed")
    def test_real_pdflatex_reports_an_original_that_never_compiled(self):
        # U+200B is not set up for use with inputenc, so the ORIGINAL fails to
        # compile. That is not held against the edit: the clean is accepted.
        p = self.write("doc.tex", STANDALONE % ZWSP)
        code, out, err = self.run_cli(["clean", "--no-backup", "--json", p])
        self.assertEqual(code, 0, out + err)
        rep = json.loads(out)
        self.assertEqual(rep["compile_verify"],
                         {"attempted": True, "before_ok": False, "after_ok": True})
        self.assertNotIn(ZWSP.encode(), self.read(p))


# =============================================================================
# Report / measurement (Section 8).
# =============================================================================

class Report(TempTree):

    def test_inspect_never_writes_and_counts_accurately(self):
        p = self.write("a.tex", "a%sb%sc%s\u0645%s\u06cc\n" % (ZWSP, SHY, ZWSP, ZWNJ))
        before = self.read(p)
        code, out, err = self.run_cli(["inspect", "--json", p])
        self.assertEqual(code, 0)
        self.assertEqual(self.read(p), before)
        self.assertFalse(os.path.exists(p + ".bak"))
        rep = json.loads(out)
        self.assertEqual(rep["status"], "unchanged")
        self.assertEqual(rep["counts"]["bucket1_stripped"], {"U+200B": 2, "U+00AD": 1})
        self.assertEqual(rep["counts"]["zwnj_zwj_preserved"], 1)

    def test_json_change_rows_carry_position_and_name(self):
        p = self.write("a.tex", "line one\nx%sy\n" % ZWSP)
        code, out, _ = self.run_cli(["inspect", "--json", p])
        rows = json.loads(out)["changes"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], {"line": 2, "col": 2, "category": "bucket1",
                                   "action": "strip", "codepoint": "U+200B",
                                   "name": "ZERO WIDTH SPACE"})

    def test_json_aggregate_sums_counts_over_a_corpus(self):
        a = self.write("a.tex", "x%sy%sz\n" % (ZWSP, ZWSP))
        b = self.write("b.tex", "p%sq\n" % SHY)
        c = self.write("c.tex", "clean file\n")
        code, out, _ = self.run_cli(["inspect", "--json", a, b, c])
        doc = json.loads(out)
        self.assertEqual(len(doc["files"]), 3)
        self.assertEqual(doc["summary"]["bucket1_stripped"], {"U+200B": 2, "U+00AD": 1})
        self.assertEqual(doc["summary"]["files"], 3)

    def test_known_clean_fixture_yields_zero_bucket1(self):
        src = ("\\documentclass{article}\n\\begin{document}\n"
               "Plain ASCII prose with $math$ and \\verb|code|.\n"
               "\\end{document}\n")
        p = self.write("clean.tex", src)
        code, out, _ = self.run_cli(["inspect", "--json", p])
        rep = json.loads(out)
        self.assertEqual(rep["counts"]["bucket1_stripped"], {})
        self.assertEqual(rep["counts"]["zwnj_zwj_stripped"], 0)
        self.assertEqual(rep["status"], "unchanged")
        self.assertEqual(code, 0)

    def test_human_report_marks_accent_warnings(self):
        p = self.write("a.tex", "caf\u00e9 and \\author{Erd\u0151s}\n")
        code, out, _ = self.run_cli(["clean", "--no-backup", "--fold-accents",
                                     "--no-compile-verify", p])
        self.assertIn("warning", out)
        self.assertIn("skipped", out)                       # the \author one
        self.assertIn("fold_accents=1", out)                # the body one
        self.assertEqual(self.read(p).decode(), "cafe and \\author{Erd\u0151s}\n")

    def test_refusal_report_json_shape(self):
        p = self.write("a.tex", "\\begin{verbatim}\nunclosed\n")
        code, out, _ = self.run_cli(["inspect", "--json", p])
        rep = json.loads(out)
        self.assertEqual(rep["status"], "refused")
        self.assertEqual(rep["refusal"]["guard"], "lexer")
        self.assertIsInstance(rep["refusal"]["line"], int)


# =============================================================================
# CLI surface (Section 9).
# =============================================================================

class Cli(TempTree):

    def test_backup_written_by_default_and_suppressed_by_flag(self):
        p = self.write("a.tex", "x%s\n" % ZWSP)
        self.run_cli(["clean", p])
        self.assertEqual(self.read(p + ".bak"), ("x%s\n" % ZWSP).encode())

        q = self.write("b.tex", "y%s\n" % ZWSP)
        self.run_cli(["clean", "--no-backup", q])
        self.assertFalse(os.path.exists(q + ".bak"))

    def test_dash_o_writes_elsewhere_and_leaves_the_input_alone(self):
        p = self.write("a.tex", "x%s\n" % ZWSP)
        dest = os.path.join(self.dir, "out.tex")
        code, _, _ = self.run_cli(["clean", "-o", dest, p])
        self.assertEqual(code, 0)
        self.assertEqual(self.read(dest), b"x\n")
        self.assertEqual(self.read(p), ("x%s\n" % ZWSP).encode())

    def test_dash_o_rejected_for_multiple_inputs(self):
        a = self.write("a.tex", "x%s\n" % ZWSP)
        b = self.write("b.tex", "y%s\n" % ZWSP)
        code, out, err = self.run_cli(["clean", "-o", "z.tex", a, b])
        self.assertEqual(code, u.EXIT_ERROR)

    def test_exit_zero_when_all_clean_or_unchanged(self):
        a = self.write("a.tex", "nothing to do\n")
        code, _, _ = self.run_cli(["inspect", a])
        self.assertEqual(code, 0)

    def test_missing_file_is_an_error_not_a_silent_pass(self):
        code, out, err = self.run_cli(["inspect", os.path.join(self.dir, "nope.tex")])
        self.assertEqual(code, u.EXIT_ERROR)

    def test_stdin_plain_text_mode(self):
        src = ("hello%s  world   \n" % ZWSP).encode()
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(src), isatty=lambda: False)):
            buf = io.BytesIO()
            out_wrapper = mock.Mock(buffer=buf, write=lambda s: None, isatty=lambda: False)
            with mock.patch.object(sys, "stdout", out_wrapper), \
                 redirect_stderr(io.StringIO()):
                code = u.main(["clean"])
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue(), b"hello world\n")

    def test_help_text_contains_no_watermark_claim(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            try:
                u.build_parser().parse_args(["--help"])
            except SystemExit:
                pass
        self.assertNotIn("watermark", buf.getvalue().lower().replace(
            "it does not detect or remove watermarks", ""))


# =============================================================================
# Idempotence.
# =============================================================================

class Idempotence(unittest.TestCase):

    SRC = (
        "\\documentclass{article}\n"
        "\\author{Paul Erd%ss}\n"
        "\\begin{document}\n"
        "Caf%s \u2014 a dash, \u201cquotes\u201d, an ellipsis\u2026 and %s invisible.\n"
        "A stray \u2212 minus and \u00d7 times in text.\n"
        "Non\u00a0breaking and thin\u2009space.\n"
        "\\verb|raw %s stays| and $x %s+ 1$ and %% comment %s stays\n"
        "\\begin{verbatim}\nraw %s body\n\\end{verbatim}\n"
        "\\end{document}\n"
    ) % ("\u0151", "\u00e9", ZWSP, ZWSP, ZWSP, ZWSP, ZWSP)

    def test_tex_clean_is_a_fixed_point(self):
        for kw in ({}, {"fold_punctuation": True},
                   {"fold_punctuation": True, "fold_accents": True}):
            once, rep1 = clean(self.SRC, **kw)
            twice, rep2 = clean(once, **kw)
            self.assertEqual(once, twice, kw)
            self.assertEqual(rep2["changes"], [], kw)
            self.assertEqual(rep2["status"], "unchanged", kw)

    def test_plaintext_clean_is_a_fixed_point(self):
        src = ("Ragged   text\twith%s junk   \r\n"
               "\n\n\n\n"
               "and \u2014 dashes\u2026\n") % ZWSP
        for kw in ({}, {"fold_punctuation": True}):
            once, _ = clean(src, is_tex=False, **kw)
            twice, rep2 = clean(once, is_tex=False, **kw)
            self.assertEqual(once, twice, kw)
            self.assertEqual(rep2["changes"], [], kw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
