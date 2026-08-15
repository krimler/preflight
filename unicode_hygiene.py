#!/usr/bin/env python3
"""
unicode_hygiene.py -- fail-closed Unicode hygiene for plain-text and LaTeX manuscripts.

Strips invisible / zero-information format characters that AI-assisted drafting and
copy-paste leave behind in source files, and optionally folds typographic punctuation
and accented letters to their LaTeX source forms.

This is a TEXT-HYGIENE tool. It normalizes text. It does not detect, identify, or score
watermarks, and nothing here should be described as detecting or defeating one. It does
strip the whole invisible-character surface, so anything encoded in those codepoints is
gone afterwards -- a consequence of the normalization, not a capability it aims at or
reports on.

Design contract (the reason this file is structured the way it is):

    Break the tool, never the file.

    1. The input is read read-only into memory.
    2. The entire cleaned result and change report are computed in memory as an
       *edit list* -- the transform never builds an output string itself.
    3. `apply_edits` is the single place an output string is constructed.
    4. Every guard runs against the in-memory result.
    5. Only if all guards pass is anything written: temp file in the destination
       directory -> fsync -> atomic os.replace. The original is never opened for
       writing and never modified in place.

    No write primitive is reachable before step 5, so a crash, exception, or KeyError
    anywhere in steps 1-4 is structurally incapable of leaving a partial file.

Standard library only. `pdflatex` is invoked as a subprocess for the optional
compile-verify step; its absence degrades to a warning and never crashes.

Python 3.10+.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field

__version__ = "1.0.0"

TEX_SUFFIXES = (".tex", ".latex", ".ltx")


# =============================================================================
# Section 3 -- character tables.
#
# These are enumerated explicitly and deliberately NOT derived from Unicode
# general categories: Cf over-selects (it contains characters we must keep) and
# under-selects (variation selectors are Mn, soft hyphen is Pd).
# =============================================================================

def _r(lo: int, hi: int) -> set[int]:
    """Inclusive codepoint range."""
    return set(range(lo, hi + 1))


# --- 3.1 Bucket 1: strip unconditionally (outside protected regions). --------
# Genuinely invisible or zero-information. Removing these cannot change rendered
# output under pdfLaTeX.
BUCKET1: frozenset[int] = frozenset(
    {
        0x200B,  # ZERO WIDTH SPACE
        0x2060,  # WORD JOINER
        0x2061,  # FUNCTION APPLICATION
        0x2062,  # INVISIBLE TIMES
        0x2063,  # INVISIBLE SEPARATOR
        0x2064,  # INVISIBLE PLUS
        0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
        0x00AD,  # SOFT HYPHEN
        0x034F,  # COMBINING GRAPHEME JOINER
        0x180E,  # MONGOLIAN VOWEL SEPARATOR
        0x200E,  # LEFT-TO-RIGHT MARK
        0x200F,  # RIGHT-TO-LEFT MARK
    }
    | _r(0x202A, 0x202E)      # bidi embeddings/overrides: LRE RLE PDF LRO RLO
    | _r(0x2066, 0x2069)      # bidi isolates: LRI RLI FSI PDI
    | _r(0xE0000, 0xE007F)    # Unicode TAG block (entire range)
    | _r(0xFE00, 0xFE0F)      # variation selectors 1-16
    | _r(0xE0100, 0xE01EF)    # variation selectors supplement
)

# --- 3.2 Bucket 1-conditional: ZWNJ / ZWJ, context-gated. -------------------
ZWNJ = 0x200C
ZWJ = 0x200D
JOINERS: frozenset[int] = frozenset({ZWNJ, ZWJ})

# Scripts and symbol ranges in which ZWNJ/ZWJ carry meaning. Adjacency to any of
# these preserves the joiner.
_ARABIC = _r(0x0600, 0x06FF) | _r(0x0750, 0x077F) | _r(0x08A0, 0x08FF)
_INDIC = _r(0x0900, 0x0DFF)
_EMOJI = _r(0x1F300, 0x1FAFF) | _r(0x2600, 0x27BF)
JOINER_SCRIPTS: frozenset[int] = frozenset(_ARABIC | _INDIC | _EMOJI)

VS16 = 0xFE0F  # VARIATION SELECTOR-16, the emoji presentation selector

# --- 3.3 Bucket 2: folds, off by default. -----------------------------------
# LaTeX targets. The math-mode-only folds (U+2212, U+00D7) are listed here but
# can only ever fire in text mode: a stray minus or times sign that is already
# inside math lives in a protected region and is never examined at all. See the
# guard in _char_edits().
PUNCT_FOLDS_TEX: dict[str, str] = {
    "‘": "`",            # LEFT SINGLE QUOTATION MARK
    "’": "'",            # RIGHT SINGLE QUOTATION MARK
    "“": "``",           # LEFT DOUBLE QUOTATION MARK
    "”": "''",           # RIGHT DOUBLE QUOTATION MARK
    "‚": ",",            # SINGLE LOW-9 QUOTATION MARK  (documented choice)
    "„": ",,",           # DOUBLE LOW-9 QUOTATION MARK  (documented choice)
    "–": "--",           # EN DASH
    "—": "---",          # EM DASH
    "―": "---",          # HORIZONTAL BAR
    "−": "$-$",          # MINUS SIGN        (text mode only -- see above)
    "×": "$\\times$",    # MULTIPLICATION SIGN (text mode only -- see above)
    "…": "\\ldots{}",    # HORIZONTAL ELLIPSIS
    " ": "~",            # NO-BREAK SPACE -> pdfLaTeX tie, preserves semantics
    " ": "\\,",          # THIN SPACE
    " ": " ",            # FIGURE SPACE
    " ": " ",            # PUNCTUATION SPACE
    " ": " ",            # EN SPACE
    " ": " ",            # EM SPACE
}

# Plain text has no LaTeX to emit, so the same source characters fold to their
# conventional ASCII renderings instead. Documented choice; see README.
PUNCT_FOLDS_TXT: dict[str, str] = {
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "‚": ",",
    "„": ",,",
    "–": "-",
    "—": "--",
    "―": "--",
    "−": "-",
    "×": "x",
    "…": "...",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
}

# Letters with no combining decomposition; NFKD alone will not fold these.
ACCENT_SPECIALS: dict[str, str] = {
    "ß": "ss", "ẞ": "SS",     # sharp s
    "æ": "ae", "Æ": "AE",
    "œ": "oe", "Œ": "OE",
    "ø": "o", "Ø": "O",
    "đ": "d", "Đ": "D",
    "ð": "d", "Ð": "D",
    "ł": "l", "Ł": "L",
    "þ": "th", "Þ": "Th",
    "ı": "i", "İ": "I",
    "ŋ": "ng", "Ŋ": "NG",
    "ħ": "h", "Ħ": "H",
    "ŧ": "t", "Ŧ": "T",
    "ĸ": "k", "ſ": "s", "ƒ": "f",
}


def fold_accent(ch: str) -> str | None:
    """Return the ASCII fold of a single accented letter, or None if it does not fold."""
    if ch.isascii():
        return None
    if ch in ACCENT_SPECIALS:
        return ACCENT_SPECIALS[ch]
    if not unicodedata.category(ch).startswith("L"):
        return None
    decomposed = unicodedata.normalize("NFKD", ch)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    if stripped and stripped != ch and stripped.isascii():
        return stripped
    return None


def fmt_cp(ch: str) -> str:
    return "U+%04X" % ord(ch)


def cp_name(ch: str) -> str:
    return unicodedata.name(ch, "<unnamed>")


# =============================================================================
# Errors
# =============================================================================

class Refusal(Exception):
    """A guard tripped. The original file must be left untouched."""

    def __init__(self, guard: str, detail: str, line: int | None = None, col: int | None = None):
        super().__init__(detail)
        self.guard = guard
        self.detail = detail
        self.line = line
        self.col = col

    def as_dict(self) -> dict:
        return {"guard": self.guard, "line": self.line, "col": self.col, "detail": self.detail}


class LexRefusal(Refusal):
    """The lexer reached a state it cannot resolve. Carries a conservative partial result."""

    def __init__(self, detail: str, pos: int, partial: "LexResult",
                 line: int | None = None, col: int | None = None):
        super().__init__("lexer", detail, line, col)
        self.pos = pos
        self.partial = partial


# =============================================================================
# Line/column index
# =============================================================================

class LineIndex:
    """Bidirectional 1-based line/column <-> 0-based offset mapping."""

    def __init__(self, text: str):
        self.starts = [0]
        for i, c in enumerate(text):
            if c == "\n":
                self.starts.append(i + 1)
        self.length = len(text)

    def linecol(self, pos: int) -> tuple[int, int]:
        line = bisect.bisect_right(self.starts, pos)
        return line, pos - self.starts[line - 1] + 1

    def pos(self, line: int, col: int) -> int:
        if not (1 <= line <= len(self.starts)):
            raise Refusal("change_exactness", f"report references line {line}, which does not exist")
        return self.starts[line - 1] + col - 1


# =============================================================================
# Section 5.1 -- protected-region lexer.
#
# State model: a stack of frames over a fixed alphabet of kinds.
#   * The TOP frame selects the scanner mode (verbatim wins over comment wins
#     over normal). This ordering is what keeps `%` inside \verb from being read
#     as a comment and `\end{other}` inside verbatim from closing anything.
#   * The WHOLE stack decides protection: a position is protected iff any frame
#     on the stack is protective. That single rule is why nested $...$ inside
#     \text{} inside align is fully protected for free -- once a math frame is on
#     the stack we never re-enter text mode. This is the conservative branch the
#     spec permits.
#
# Two masks come out, because the two gates differ in kind:
#   protected[i]     -- no edit of any sort may touch this index.
#   accent_locked[i] -- accent folds forbidden; Bucket-1 stripping still applies.
# Stripping U+200B out of an author name is correct and desirable. Folding
# Erdos-with-a-double-acute in that same author name is silent data loss, and is
# exactly the failure this tool exists to not produce.
# =============================================================================

_BASE_VERBATIM_ENVS = {
    "verbatim", "Verbatim", "BVerbatim", "LVerbatim",
    "lstlisting", "minted", "alltt", "comment", "listing",
}
_BASE_MATH_ENVS = {
    "equation", "align", "gather", "multline", "flalign", "eqnarray",
    "math", "displaymath", "array", "cases", "dcases", "split", "alignat",
    "aligned", "gathered", "alignedat", "subequations", "IEEEeqnarray",
    "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "smallmatrix",
}


def _with_stars(names: set[str]) -> frozenset[str]:
    return frozenset(names | {n + "*" for n in names})


VERBATIM_ENVS = _with_stars(_BASE_VERBATIM_ENVS)
MATH_ENVS = _with_stars(_BASE_MATH_ENVS)

# Section 4: contexts in which an accent fold is silent data loss, even with the
# flag explicitly passed.
ACCENT_LOCK_CMDS = frozenset({
    "author", "title", "thanks", "date",
    "cite", "citep", "citet", "citealp", "citealt", "citeauthor", "citeyear",
    "Cite", "Citep", "Citet", "Citealp", "Citealt", "Citeauthor", "Citeyear",
    "textcite", "parencite", "autocite", "footcite", "nocite",
    "bibliography", "bibitem",
})
ACCENT_LOCK_ENVS = frozenset({"thebibliography"})

# Regions in which nothing may be edited at all.
PROTECTIVE_KINDS = frozenset({
    "verb", "verbatim",
    "math_dollar", "math_ddollar", "math_paren", "math_bracket", "math_env",
})

# Regions in which no FOLD may happen, though stripping is allowed. A comment
# does not render, so folding inside one buys nothing and risks rewriting
# commented-out source that gets uncommented later.
FOLD_LOCK_KINDS = PROTECTIVE_KINDS | frozenset({"comment"})

# Regions the lexer emits a span for, so the output re-lex can compare structure.
STRUCTURAL_KINDS = FOLD_LOCK_KINDS

_LETTERS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


@dataclass
class Span:
    kind: str
    depth: int
    start: int
    end: int
    text: str = ""


@dataclass
class _Frame:
    kind: str
    start: int
    name: str | None = None
    delim: str | None = None
    depth: int = 0
    arg_of: str | None = None
    end_token: str | None = None
    brace_depth: int = 0


@dataclass
class LexResult:
    protected: bytearray        # no edit of any kind
    fold_locked: bytearray      # no fold; stripping still allowed (comments)
    accent_locked: bytearray    # no accent fold
    spans: list[Span]
    warnings: list[dict]
    lock_ranges: list[tuple[int, int, str]] = field(default_factory=list)

    def lock_reason(self, pos: int) -> str:
        """Human name for why `pos` is accent-locked -- the innermost enclosing reason."""
        best, width = "protected region", None
        for start, end, reason in self.lock_ranges:
            if start <= pos < end and (width is None or end - start < width):
                best, width = reason, end - start
        return best


class _Lexer:
    def __init__(self, text: str):
        self.t = text
        self.n = len(text)
        self.i = 0
        self.stack: list[_Frame] = [_Frame("doc", 0)]
        self.spans: list[Span] = []
        self.acc_ranges: list[tuple[int, int, str]] = []
        self.warnings: list[dict] = []
        self.pending: str | None = None      # control word awaiting its braced argument
        self.pending_nl = 0
        self.idx = LineIndex(text)

    # -- frame bookkeeping ---------------------------------------------------

    def _push(self, f: _Frame) -> None:
        f.depth = len(self.stack)
        self.stack.append(f)

    def _pop(self, end: int) -> _Frame:
        f = self.stack.pop()
        if f.kind in STRUCTURAL_KINDS:
            self.spans.append(Span(f.kind, f.depth, f.start, end))
        elif f.kind == "group" and f.arg_of:
            self.acc_ranges.append((f.start, end, "\\%s{...}" % f.arg_of))
        elif f.kind == "env" and f.name in ACCENT_LOCK_ENVS:
            self.acc_ranges.append((f.start, end, "%s environment" % f.name))
        elif f.kind == "bibitem":
            self.acc_ranges.append((f.start, end, "\\bibitem line"))
        return f

    def _warn(self, pos: int, detail: str) -> None:
        line, col = self.idx.linecol(pos)
        self.warnings.append({"line": line, "col": col, "detail": detail})

    def _refuse(self, pos: int, detail: str):
        line, col = self.idx.linecol(pos)
        partial = self._finalize(refusal_pos=pos)
        raise LexRefusal(detail, pos, partial, line, col)

    # -- finalization --------------------------------------------------------

    def _finalize(self, refusal_pos: int | None = None) -> LexResult:
        protected = bytearray(self.n)
        fold_locked = bytearray(self.n)
        accent_locked = bytearray(self.n)
        lock_ranges: list[tuple[int, int, str]] = []

        for s in self.spans:
            lo, hi = max(0, s.start), min(self.n, s.end)
            if s.kind in PROTECTIVE_KINDS:
                for k in range(lo, hi):
                    protected[k] = 1
            if s.kind in FOLD_LOCK_KINDS:
                for k in range(lo, hi):
                    fold_locked[k] = 1
            if s.kind == "comment":
                lock_ranges.append((s.start, s.end, "comment"))

        accent_locked[:] = fold_locked
        for start, end, reason in self.acc_ranges:
            lock_ranges.append((start, end, reason))
            for k in range(max(0, start), min(self.n, end)):
                accent_locked[k] = 1

        if refusal_pos is not None:
            # Conservative tail: everything from the point the lexer lost
            # confidence onward is treated as protected, so --force-lexer still
            # cleans only the prefix it understood.
            for k in range(max(0, refusal_pos), self.n):
                protected[k] = 1
                fold_locked[k] = 1
                accent_locked[k] = 1

        spans = sorted(self.spans, key=lambda s: (s.start, s.depth))
        return LexResult(protected, fold_locked, accent_locked, spans,
                         self.warnings, lock_ranges)

    # -- scanning helpers ----------------------------------------------------

    def _skip_spaces(self, p: int) -> int:
        while p < self.n and self.t[p] in " \t":
            p += 1
        return p

    def _scan_balanced(self, p: int, opener: str, closer: str) -> int | None:
        """p points at `opener`. Return index just past the matching closer, or None."""
        depth = 0
        while p < self.n:
            c = self.t[p]
            if c == "\\" and p + 1 < self.n:
                p += 2
                continue
            if c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    return p + 1
            p += 1
        return None

    def _read_env_name(self, p: int) -> tuple[str, int] | None:
        """p points just past \\begin or \\end. Read `{name}`; return (name, next_index)."""
        p = self._skip_spaces(p)
        if p >= self.n or self.t[p] != "{":
            return None
        q = p + 1
        name_chars = []
        while q < self.n and self.t[q] != "}":
            if self.t[q] == "\n":
                return None
            name_chars.append(self.t[q])
            q += 1
        if q >= self.n:
            return None
        return "".join(name_chars).strip(), q + 1

    def _open_inline_verb(self, cmd_start: int, p: int, cmd: str) -> int:
        """p points just past the command name (and any options). Read the delimiter."""
        if p >= self.n:
            self._refuse(cmd_start, r"unterminated \%s: end of file before delimiter" % cmd)
        d = self.t[p]
        if d in _LETTERS or d == "*" or d == "\n":
            self._refuse(
                cmd_start,
                r"\%s delimiter must be a single non-letter, non-'*' character; found %r" % (cmd, d),
            )
        f = _Frame("verb", cmd_start, name=cmd, delim=d)
        if d == "{":
            f.brace_depth = 1
        self._push(f)
        return p + 1

    # -- the main loop -------------------------------------------------------

    def run(self) -> LexResult:
        t, n = self.t, self.n
        while self.i < n:
            top = self.stack[-1]
            kind = top.kind
            c = t[self.i]

            # ---- mode: VERBATIM (wins over everything) ----------------------
            if kind == "verb":
                if top.delim == "{":
                    if c == "\\" and self.i + 1 < n:
                        self.i += 2
                        continue
                    if c == "{":
                        top.brace_depth += 1
                    elif c == "}":
                        top.brace_depth -= 1
                        if top.brace_depth == 0:
                            self._pop(self.i + 1)
                            self.i += 1
                            continue
                    self.i += 1
                    continue
                if c == "\n":
                    self._refuse(
                        top.start,
                        r"unterminated \%s: no closing %r before end of line" % (top.name, top.delim),
                    )
                if c == top.delim:
                    self._pop(self.i + 1)
                    self.i += 1
                    continue
                self.i += 1
                continue

            if kind == "verbatim":
                assert top.end_token is not None
                if c == "\\" and t.startswith(top.end_token, self.i):
                    end = self.i + len(top.end_token)
                    self._pop(end)
                    self.i = end
                    continue
                self.i += 1
                continue

            # ---- mode: COMMENT ---------------------------------------------
            if kind == "comment":
                if c == "\n":
                    self._pop(self.i)   # the newline itself is not part of the comment
                    continue            # reprocess the newline in normal mode
                self.i += 1
                continue

            # ---- mode: NORMAL ----------------------------------------------
            if c == "\\":
                self._handle_backslash()
                continue

            if c == "%":
                self._push(_Frame("comment", self.i))
                self.pending = None
                self.i += 1
                continue

            if c == "$":
                self._handle_dollar()
                continue

            if c == "{":
                f = _Frame("group", self.i, arg_of=self.pending)
                self.pending = None
                self._push(f)
                self.i += 1
                continue

            if c == "}":
                if self.stack[-1].kind == "group":
                    self._pop(self.i + 1)
                else:
                    # Brace imbalance cannot change which regions are protected --
                    # `group` is non-protective -- so this is a warning, not a
                    # refusal. An unclosed accent-locked group simply stays locked
                    # to end of file, which is the conservative direction.
                    self._warn(self.i, "unbalanced '}' with no open group; ignored")
                self.pending = None
                self.i += 1
                continue

            if c == "[" and self.pending:
                # Optional argument of a pending command, e.g. \cite[p.~3]{key}.
                end = self._scan_balanced(self.i, "[", "]")
                if end is None:
                    self.pending = None
                    self.i += 1
                    continue
                self.i = end
                continue

            if c == "\n":
                if self.stack[-1].kind == "bibitem":
                    self._pop(self.i)
                self.pending_nl += 1
                if self.pending_nl > 1:
                    self.pending = None
                self.i += 1
                continue

            if c not in " \t":
                self.pending = None
            self.i += 1

        return self._finish()

    def _handle_backslash(self) -> None:
        t, n = self.t, self.n
        start = self.i
        if start + 1 >= n:
            self._refuse(start, "dangling backslash at end of file")
        d = t[start + 1]

        if d not in _LETTERS:
            # Control symbol: exactly two characters, and inert as far as region
            # structure goes. This is what makes \% not a comment, \$ not math,
            # \{ not a group, \\ not an escape of whatever follows.
            if d == "(":
                self._push(_Frame("math_paren", start))
                self.i = start + 2
                return
            if d == ")":
                if self.stack[-1].kind == "math_paren":
                    self._pop(start + 2)
                    self.i = start + 2
                    return
                self._refuse(start, r"unmatched \) with no open \( math region")
            if d == "[":
                self._push(_Frame("math_bracket", start))
                self.i = start + 2
                return
            if d == "]":
                if self.stack[-1].kind == "math_bracket":
                    self._pop(start + 2)
                    self.i = start + 2
                    return
                self._refuse(start, r"unmatched \] with no open \[ math region")
            self.pending = None
            self.i = start + 2
            return

        # Control word.
        j = start + 1
        while j < n and t[j] in _LETTERS:
            j += 1
        name = t[start + 1:j]
        if j < n and t[j] == "*":
            j += 1
            starred = name + "*"
        else:
            starred = name

        if name == "verb":
            self.i = self._open_inline_verb(start, j, starred)
            self.pending = None
            return

        if name == "lstinline":
            p = self._skip_spaces(j)
            if p < n and t[p] == "[":
                end = self._scan_balanced(p, "[", "]")
                if end is None:
                    self._refuse(start, r"unterminated optional argument of \lstinline")
                p = self._skip_spaces(end)
            self.i = self._open_inline_verb(start, p, "lstinline")
            self.pending = None
            return

        if name == "mintinline":
            p = self._skip_spaces(j)
            if p < n and t[p] == "{":
                end = self._scan_balanced(p, "{", "}")
                if end is None:
                    self._refuse(start, r"unterminated language argument of \mintinline")
                p = self._skip_spaces(end)
            self.i = self._open_inline_verb(start, p, "mintinline")
            self.pending = None
            return

        if name == "begin":
            got = self._read_env_name(j)
            if got is None:
                self._refuse(start, r"malformed \begin: expected {name}")
            env, after = got
            if env in VERBATIM_ENVS:
                f = _Frame("verbatim", start, name=env)
                f.end_token = "\\end{%s}" % env
                self._push(f)
            elif env in MATH_ENVS:
                self._push(_Frame("math_env", start, name=env))
            else:
                self._push(_Frame("env", start, name=env))
            self.pending = None
            self.i = after
            return

        if name == "end":
            got = self._read_env_name(j)
            if got is None:
                self._refuse(start, r"malformed \end: expected {name}")
            env, after = got
            # Pop any open groups/bibitem lines first: brace imbalance is a
            # warning, and it must not mask a legitimate environment match.
            while self.stack[-1].kind in ("group", "bibitem"):
                self._warn(self.stack[-1].start, r"group left open at \end{%s}; closed implicitly" % env)
                self._pop(start)
            top = self.stack[-1]
            if top.kind in ("env", "math_env") and top.name == env:
                self._pop(after)
                self.pending = None
                self.i = after
                return
            if top.kind == "doc":
                self._refuse(start, r"\end{%s} with no matching \begin" % env)
            self._refuse(
                start,
                r"\end{%s} does not match open \begin{%s} (line %d)"
                % (env, top.name, self.idx.linecol(top.start)[0]),
            )

        if name == "bibitem":
            self._push(_Frame("bibitem", start))
            self.pending = "bibitem"
            self.pending_nl = 0
            self.i = j
            return

        if name in ACCENT_LOCK_CMDS:
            self.pending = name
            self.pending_nl = 0
            self.i = j
            return

        self.pending = None
        self.i = j

    def _handle_dollar(self) -> None:
        t, n = self.t, self.n
        i = self.i
        top = self.stack[-1]
        if top.kind == "math_dollar":
            self._pop(i + 1)          # a single $ closes inline math; do not peek
            self.i = i + 1
            return
        if top.kind == "math_ddollar":
            if t.startswith("$$", i):
                self._pop(i + 2)
                self.i = i + 2
                return
            self.i = i + 1
            return
        if top.kind in ("math_env", "math_paren", "math_bracket"):
            # Illegal TeX, but the span is already protected, so ignoring cannot
            # cause a wrong edit. Refusing here would only manufacture false
            # refusals on files that compile fine.
            self.i = i + 1
            return
        if t.startswith("$$", i):
            self._push(_Frame("math_ddollar", i))
            self.i = i + 2
        else:
            self._push(_Frame("math_dollar", i))
            self.i = i + 1
        self.pending = None

    def _finish(self) -> LexResult:
        while len(self.stack) > 1:
            f = self.stack[-1]
            if f.kind == "verb":
                self._refuse(f.start, r"unterminated \%s at end of file" % f.name)
            if f.kind == "verbatim":
                self._refuse(f.start, r"unterminated \begin{%s}: no matching \end" % f.name)
            if f.kind == "math_env":
                self._refuse(f.start, r"unterminated math environment \begin{%s}" % f.name)
            if f.kind in ("math_dollar", "math_ddollar", "math_paren", "math_bracket"):
                sym = {"math_dollar": "$", "math_ddollar": "$$",
                       "math_paren": "\\(", "math_bracket": "\\["}[f.kind]
                self._refuse(f.start, "unterminated math region opened by %s" % sym)
            if f.kind == "env":
                self._refuse(f.start, r"unterminated \begin{%s}: no matching \end" % f.name)
            if f.kind == "comment":
                self._pop(self.n)
                continue
            if f.kind == "group":
                self._warn(f.start, "group left open at end of file; treated as extending to EOF")
                self._pop(self.n)
                continue
            self._pop(self.n)
        return self._finalize()


def lex(text: str) -> LexResult:
    """Lex a LaTeX source. Raises LexRefusal (which carries a conservative partial)."""
    return _Lexer(text).run()


def lex_plain(text: str) -> LexResult:
    """Plain text has no protected regions."""
    n = len(text)
    return LexResult(bytearray(n), bytearray(n), bytearray(n), [], [])


# =============================================================================
# Edits -- the only representation of change, and the only way output is built.
# =============================================================================

@dataclass
class Edit:
    start: int
    end: int
    replacement: str
    category: str
    action: str
    before: str
    after: str
    codepoint: str | None = None
    name: str | None = None


def apply_edits(text: str, edits: list[Edit]) -> tuple[str, list[tuple[int, int]]]:
    """Build the output. Returns (output, [(out_start, out_end) per edit])."""
    ordered = sorted(edits, key=lambda e: e.start)
    out: list[str] = []
    out_ranges: list[tuple[int, int]] = []
    cursor = 0
    length = 0
    for e in ordered:
        if e.start < cursor:
            raise Refusal("internal", "overlapping edits at offset %d" % e.start)
        chunk = text[cursor:e.start]
        out.append(chunk)
        length += len(chunk)
        out.append(e.replacement)
        out_ranges.append((length, length + len(e.replacement)))
        length += len(e.replacement)
        cursor = e.end
    out.append(text[cursor:])
    return "".join(out), out_ranges


def edit_row(e: Edit, idx: LineIndex) -> dict:
    """The machine-readable change record. Section 5.3 reconstructs from these."""
    line, col = idx.linecol(e.start)
    row = {"line": line, "col": col, "category": e.category, "action": e.action}
    if e.action == "strip" and e.codepoint:
        row["codepoint"] = e.codepoint
        row["name"] = e.name
    else:
        row["before"] = e.before
        row["after"] = e.after
    return row


# =============================================================================
# Transform -- pure. Emits edits and report rows; never builds an output string.
# =============================================================================

@dataclass
class Options:
    fold_punctuation: bool = False
    fold_accents: bool = False
    encoding: str | None = None
    backup: bool = True
    compile_verify: str = "auto"     # auto | always | never
    force_relex: bool = False
    force_change_exactness: bool = False
    force_encoding: bool = False
    force_lexer: bool = False


def _neighbour(text: str, lexres: LexResult, i: int, step: int) -> str:
    """
    Classify the neighbour of a joiner at index i, skipping other Bucket-1
    characters. Returns 'script' | 'plain' | 'uncertain'.
    """
    j = i + step
    n = len(text)
    while 0 <= j < n:
        if lexres.protected[j]:
            return "uncertain"          # protected-region boundary: ambiguous, preserve
        cp = ord(text[j])
        if cp in BUCKET1:
            j += step
            continue
        if cp in JOINER_SCRIPTS:
            return "script"
        # A base character carrying the emoji presentation selector counts as
        # pictographic even if its own codepoint is outside the emoji blocks.
        if j + 1 < n and ord(text[j + 1]) == VS16:
            return "script"
        return "plain"
    return "plain"                       # file boundary: no neighbour, no reason to preserve


def compute_edits(
    text: str, lexres: LexResult, opts: Options, is_tex: bool
) -> tuple[list[Edit], list[dict], dict]:
    """Returns (edits, warnings, extra_counts)."""
    idx = LineIndex(text)
    edits: list[Edit] = []
    warnings: list[dict] = []
    counts = {"zwnj_zwj_preserved": 0}
    punct_map = PUNCT_FOLDS_TEX if is_tex else PUNCT_FOLDS_TXT

    def warn(pos: int, detail: str) -> None:
        line, col = idx.linecol(pos)
        warnings.append({"line": line, "col": col, "detail": detail})

    for i, ch in enumerate(text):
        if lexres.protected[i]:
            continue
        cp = ord(ch)

        if cp in BUCKET1:
            edits.append(Edit(i, i + 1, "", "bucket1", "strip", ch, "",
                              codepoint=fmt_cp(ch), name=cp_name(ch)))
            continue

        if cp in JOINERS:
            left = _neighbour(text, lexres, i, -1)
            right = _neighbour(text, lexres, i, +1)
            if left in ("script", "uncertain") or right in ("script", "uncertain"):
                counts["zwnj_zwj_preserved"] += 1
                continue
            edits.append(Edit(i, i + 1, "", "zwnj_zwj", "strip", ch, "",
                              codepoint=fmt_cp(ch), name=cp_name(ch)))
            continue

        if opts.fold_punctuation and ch in punct_map and not lexres.fold_locked[i]:
            # Note the math interaction: U+2212 and U+00D7 fold to $-$ and
            # $\times$, but an occurrence already inside math mode lives in a
            # protected region and was skipped above, so only stray text-mode
            # occurrences can ever reach this line.
            rep = punct_map[ch]
            edits.append(Edit(i, i + 1, rep, "fold_punctuation", "fold", ch, rep,
                              codepoint=fmt_cp(ch), name=cp_name(ch)))
            continue

        if opts.fold_accents:
            folded = fold_accent(ch)
            if folded is not None:
                if lexres.accent_locked[i]:
                    warn(i, "accent fold %s->%s skipped: %s context"
                         % (ch, folded, lexres.lock_reason(i)))
                    continue
                # Section 4: under pdfLaTeX with inputenc an accented source
                # character is intended content. Every fold is warned about.
                warn(i, "accent fold %s->%s (source accents are usually intentional "
                        "under pdfLaTeX/inputenc)" % (ch, folded))
                edits.append(Edit(i, i + 1, folded, "fold_accents", "fold", ch, folded))
                continue

    if not is_tex:
        edits = _plaintext_whitespace(text, edits, counts)

    edits.sort(key=lambda e: e.start)
    return edits, warnings, counts


# --- 3.4 plain-text-only whitespace normalization ----------------------------

def _plaintext_whitespace(text: str, edits: list[Edit], counts: dict) -> list[Edit]:
    """
    On .txt/stdin only: normalize CRLF/CR to LF, collapse space/tab runs, strip
    trailing whitespace per line, collapse 3+ blank lines to 2.

    Computed as edits over the ORIGINAL text so the change-exactness guard still
    holds. Existing edits are treated as transparent: a Bucket-1 character being
    deleted between two spaces does not break the whitespace run it sits in, and
    a fold whose replacement is a space participates in collapsing (its
    replacement is rewritten to '' rather than emitting an overlapping edit).
    """
    n = len(text)
    covered = bytearray(n)
    by_start: dict[int, Edit] = {}
    for e in edits:
        by_start[e.start] = e
        for k in range(e.start, e.end):
            covered[k] = 1

    ws_edits: list[Edit] = []
    counts["whitespace_normalized"] = 0

    def add(start: int, end: int, rep: str) -> None:
        if text[start:end] == rep:
            return
        ws_edits.append(Edit(start, end, rep, "whitespace", "normalize",
                             text[start:end], rep))
        counts["whitespace_normalized"] += 1

    # (a) CR handling.
    i = 0
    while i < n:
        if text[i] == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                add(i, i + 1, "")             # drop the CR of a CRLF
            else:
                add(i, i + 1, "\n")           # lone CR becomes LF
        i += 1
    cr_positions = {e.start for e in ws_edits}

    def is_slot(k: int) -> bool:
        """
        Position participates in a whitespace run: it emits nothing, or a single
        space. A fold that emits real content (U+2014 -> '--') is NOT a slot.
        """
        if k in cr_positions:
            return text[k] == "\r" and k + 1 < n and text[k + 1] == "\n"
        if covered[k]:
            e = by_start.get(k)
            return e is not None and e.replacement in ("", " ")
        return text[k] in " \t"

    def slot_is_ws(k: int) -> bool:
        """Position contributes an actual space/tab to the output."""
        if k in cr_positions:
            return False
        if covered[k]:
            e = by_start.get(k)
            return e is not None and e.replacement == " "
        return text[k] in " \t"

    def kill_slot(k: int) -> None:
        if covered[k]:
            e = by_start[k]
            e.replacement = ""
            e.after = ""
        else:
            add(k, k + 1, "")

    # (b) per-line trailing strip and interior run collapse.
    line_bounds: list[tuple[int, int]] = []
    start = 0
    for k in range(n):
        if text[k] == "\n":
            line_bounds.append((start, k))
            start = k + 1
    line_bounds.append((start, n))

    blank: list[bool] = []
    for ls, le in line_bounds:
        # Last position holding real content (anything that is not a whitespace slot).
        last_content = -1
        for k in range(ls, le):
            if not is_slot(k):
                last_content = k
        blank.append(last_content < 0)

        # Trailing region: every whitespace slot after the last content char goes.
        for k in range(max(ls, last_content + 1), le):
            if slot_is_ws(k):
                kill_slot(k)

        # Interior runs: keep the first whitespace slot, drop the rest.
        k = ls
        while k <= last_content:
            if not is_slot(k):
                k += 1
                continue
            j = k
            while j <= last_content and is_slot(j):
                j += 1
            slots = [p for p in range(k, j) if slot_is_ws(p)]
            if slots:
                first = slots[0]
                if not covered[first] and text[first] == "\t":
                    add(first, first + 1, " ")
                for p in slots[1:]:
                    kill_slot(p)
            k = j

    # (c) collapse 3+ blank lines to 2 by deleting the surplus newlines.
    run_start = None
    for li in range(len(blank) + 1):
        is_blank = blank[li] if li < len(blank) else False
        if is_blank and run_start is None:
            run_start = li
        elif not is_blank and run_start is not None:
            run_len = li - run_start
            if run_len >= 3:
                for extra in range(run_start + 2, li):
                    ls, le = line_bounds[extra]
                    if le < n and text[le] == "\n":
                        add(le, le + 1, "")
            run_start = None

    merged = edits + ws_edits
    merged.sort(key=lambda e: e.start)
    for a, b in zip(merged, merged[1:]):
        if b.start < a.end:
            raise Refusal("internal", "whitespace normalization produced overlapping edits")
    # Drop no-op edits (a fold rewritten to '' that was already '' etc.).
    return [e for e in merged if not (e.start == e.end and e.replacement == "")]


# =============================================================================
# Section 5 -- guards.
# =============================================================================

def decode_strict(data: bytes, explicit: str | None, force: bool) -> tuple[str, str, list[str]]:
    """
    Section 5.4. Returns (text, encoding_used, notes). Never errors='replace'.

    The BOM is deliberately NOT consumed by the codec: decoding as plain utf-8
    leaves U+FEFF in the text where Bucket 1 removes it, which is the point.
    """
    notes: list[str] = []
    if explicit:
        try:
            return data.decode(explicit), explicit, notes
        except (UnicodeDecodeError, LookupError) as exc:
            raise Refusal("encoding", "cannot decode as %s: %s" % (explicit, exc))
    try:
        return data.decode("utf-8"), "utf-8", notes
    except UnicodeDecodeError as exc:
        pass
    for bom, enc in ((b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
                     (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if data.startswith(bom):
            try:
                text = data[len(bom):].decode(enc)
                notes.append("decoded as %s from BOM" % enc)
                return text, enc, notes
            except UnicodeDecodeError as exc:
                raise Refusal("encoding", "BOM declares %s but content does not decode: %s" % (enc, exc))
    if force:
        # latin-1 round-trips every byte, so untouched regions re-encode to the
        # exact original bytes. Lossless, but the character identities are a
        # guess -- hence the loud warning at the call site.
        return data.decode("latin-1"), "latin-1", ["--force-encoding: decoded as latin-1"]
    raise Refusal(
        "encoding",
        "input is not valid UTF-8 and declares no BOM; pass --encoding NAME "
        "(refusing rather than force-decoding, which would itself corrupt)",
    )


def relex_guard(out_text: str, in_spans: list[Span], out_ranges: list[tuple[int, int]],
                edits: list[Edit], forced_lexer: bool = False) -> None:
    """
    Section 5.2. Re-lex the output and require the protected-region structure to
    be identical to the input's.

    One allowance: a logged fold may legitimately INTRODUCE a protected region --
    U+2212 folds to `$-$`, which is new math. Spans lying entirely inside the
    output extent of a logged fold replacement are therefore excluded from the
    comparison; everything else must match by kind, depth, and exact text.
    """
    try:
        out_lex = lex(out_text)
    except LexRefusal as exc:
        if not forced_lexer:
            raise Refusal("relex", "cleaned output no longer lexes: %s" % exc.detail)
        # The input did not fully lex either -- the user waived that with
        # --force-lexer -- so compare the understood prefixes instead. The output
        # must still lose confidence in the same way the input did.
        out_lex = exc.partial

    ordered = sorted(edits, key=lambda e: e.start)
    introduced = [rng for rng, e in zip(out_ranges, ordered) if e.action == "fold"]

    def is_introduced(s: Span) -> bool:
        return any(lo <= s.start and s.end <= hi for lo, hi in introduced)

    # Comments are structurally tracked but editable, so their text legitimately
    # changes. Their kind/depth/ordering must still match, and their content is
    # covered by the change-exactness guard.
    def cmp_text(kind: str, text: str) -> str | None:
        return text if kind in PROTECTIVE_KINDS else None

    got = [(s.kind, s.depth, cmp_text(s.kind, out_text[s.start:s.end]))
           for s in out_lex.spans if not is_introduced(s)]
    want = [(s.kind, s.depth, cmp_text(s.kind, s.text)) for s in in_spans]

    if len(got) != len(want):
        raise Refusal(
            "relex",
            "protected-region count changed: input has %d, output has %d" % (len(want), len(got)),
        )
    for k, (a, b) in enumerate(zip(want, got)):
        if a[0] != b[0] or a[1] != b[1]:
            raise Refusal(
                "relex",
                "protected region #%d changed kind/depth: %s@%d -> %s@%d" % (k + 1, a[0], a[1], b[0], b[1]),
            )
        if a[2] != b[2]:
            raise Refusal("relex", "content of protected region #%d (%s) changed" % (k + 1, a[0]))


def change_exactness_guard(in_text: str, out_text: str, rows: list[dict]) -> None:
    """
    Section 5.3. Reconstruct the output from the SERIALIZED report rows -- the same
    line/col/before/after the user and the JSON see -- and require it to equal the
    actual output exactly.

    Reconstructing from the report rather than from the internal Edit objects is
    deliberate: replaying the same objects the applier consumed would prove
    nothing. Going through line/col and an independently built line index turns a
    bug in the transform, the logger, or the column arithmetic into a refusal.
    """
    idx = LineIndex(in_text)
    spliced: list[tuple[int, int, str]] = []
    for row in rows:
        pos = idx.pos(row["line"], row["col"])
        if row["action"] == "strip" and "codepoint" in row:
            before = chr(int(row["codepoint"][2:], 16))
            after = ""
        else:
            before, after = row["before"], row["after"]
        if in_text[pos:pos + len(before)] != before:
            raise Refusal(
                "change_exactness",
                "report row at line %d col %d claims %r but the input holds %r"
                % (row["line"], row["col"], before, in_text[pos:pos + len(before)]),
                row["line"], row["col"],
            )
        spliced.append((pos, pos + len(before), after))

    spliced.sort()
    parts: list[str] = []
    cursor = 0
    for start, end, rep in spliced:
        if start < cursor:
            raise Refusal("change_exactness", "report rows describe overlapping changes at offset %d" % start)
        parts.append(in_text[cursor:start])
        parts.append(rep)
        cursor = end
    parts.append(in_text[cursor:])
    rebuilt = "".join(parts)

    if rebuilt != out_text:
        where = next((k for k in range(min(len(rebuilt), len(out_text)))
                      if rebuilt[k] != out_text[k]), min(len(rebuilt), len(out_text)))
        line, col = LineIndex(out_text).linecol(min(where, max(0, len(out_text) - 1)))
        raise Refusal(
            "change_exactness",
            "output does not match the change report (first divergence at offset %d)" % where,
            line, col,
        )


# =============================================================================
# Section 5.5 -- write safety.
# =============================================================================

# Test hook: set to a callable to simulate a crash between temp-write and
# os.replace. The original must survive.
_after_temp_write_hook = None


def read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:           # read-only, always
        return fh.read()


def atomic_write(path: str, text: str, encoding: str, backup: bool) -> str | None:
    """Temp file in the destination directory -> fsync -> atomic os.replace."""
    if os.path.islink(path):
        raise Refusal("write_safety", "destination %s is a symlink; refusing to write through it" % path)
    data = text.encode(encoding)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    backup_path = None

    if backup and os.path.exists(path):
        backup_path = path + ".bak"
        if os.path.islink(backup_path):
            raise Refusal("write_safety", "backup target %s is a symlink" % backup_path)
        _atomic_bytes(backup_path, read_bytes(path), directory)

    _atomic_bytes(path, data, directory)
    return backup_path


def _atomic_bytes(path: str, data: bytes, directory: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".uhyg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if _after_temp_write_hook is not None:
            _after_temp_write_hook(tmp)
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None and os.path.exists(tmp):
            os.unlink(tmp)
    try:
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


# =============================================================================
# Section 6 -- compile-verify.
# =============================================================================

_DOCUMENTCLASS = re.compile(
    r"(?<!\\)%.*$"                    # a comment: skipped
    r"|\\documentclass(?![a-zA-Z])"
    r"|\\input(?![a-zA-Z])"
    r"|\\include(?![a-zA-Z])",        # \includegraphics does not count
    re.M,
)

_compile_cache: dict[tuple, bool] = {}


def is_standalone(text: str) -> bool:
    """
    A standalone document: contains \\documentclass, with no unresolved
    \\input/\\include before it. Fragments legitimately do not compile alone.
    """
    for m in _DOCUMENTCLASS.finditer(text):
        tok = m.group(0)
        if tok.startswith("%"):
            continue
        if tok == "\\documentclass":
            return True
        return False       # \input or \include came first
    return False


def _run_pdflatex(tex_path: str, cwd: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="uhyg-tex-") as outdir:
        try:
            proc = subprocess.run(
                ["pdflatex", "-interaction=nonstopmode", "-halt-on-error",
                 "-output-directory", outdir, tex_path],
                cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0


def compile_verify(path: str, original: str, cleaned: str, mode: str) -> dict:
    """Returns the `compile_verify` block of the report. Raises Refusal to reject."""
    if mode == "never":
        return {"attempted": False, "reason": "flag"}
    if mode != "always" and not is_standalone(original):
        return {"attempted": False, "reason": "fragment"}
    if shutil.which("pdflatex") is None:
        return {"attempted": False, "reason": "no_pdflatex"}

    directory = os.path.dirname(os.path.abspath(path)) or "."
    key = (os.path.abspath(path), len(original))
    if key in _compile_cache:
        before_ok = _compile_cache[key]
    else:
        before_ok = _run_pdflatex(os.path.abspath(path), directory)
        _compile_cache[key] = before_ok

    # The cleaned text is compiled from a sibling temp file so relative \input,
    # graphics and .bib paths resolve exactly as they do for the real document.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="uhyg-verify-", suffix=".tex")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(cleaned)
        after_ok = _run_pdflatex(tmp, directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    result = {"attempted": True, "before_ok": before_ok, "after_ok": after_ok}
    if before_ok and not after_ok:
        raise Refusal("compile_verify",
                      "original compiles but the cleaned version does not; refusing the change")
    return result


# =============================================================================
# Section 8 -- change report.
# =============================================================================

def build_counts(edits: list[Edit], extra: dict) -> dict:
    counts: dict = {
        "bucket1_stripped": {},
        "zwnj_zwj_stripped": 0,
        "zwnj_zwj_preserved": extra.get("zwnj_zwj_preserved", 0),
        "fold_punctuation": {},
        "fold_accents": {},
    }
    if "whitespace_normalized" in extra:
        counts["whitespace_normalized"] = 0
    for e in edits:
        if e.category == "bucket1":
            counts["bucket1_stripped"][e.codepoint] = counts["bucket1_stripped"].get(e.codepoint, 0) + 1
        elif e.category == "zwnj_zwj":
            counts["zwnj_zwj_stripped"] += 1
        elif e.category == "fold_punctuation":
            counts["fold_punctuation"][e.codepoint] = counts["fold_punctuation"].get(e.codepoint, 0) + 1
        elif e.category == "fold_accents":
            key = "%s->%s" % (e.before, e.after)
            counts["fold_accents"][key] = counts["fold_accents"].get(key, 0) + 1
        elif e.category == "whitespace":
            counts["whitespace_normalized"] = counts.get("whitespace_normalized", 0) + 1
    return counts


def render_human(report: dict) -> list[str]:
    lines: list[str] = []
    name = report["file"]
    status = report["status"]
    if status == "refused":
        r = report["refusal"]
        loc = " at line %d" % r["line"] if r.get("line") else ""
        lines.append("refused %s: %s%s: %s" % (name, r["guard"], loc, r["detail"]))
        return lines

    c = report["counts"]
    bits = []
    b1 = sum(c["bucket1_stripped"].values())
    if b1:
        bits.append("bucket1=%d" % b1)
    if c["zwnj_zwj_stripped"]:
        bits.append("zwnj_zwj_stripped=%d" % c["zwnj_zwj_stripped"])
    if c["zwnj_zwj_preserved"]:
        bits.append("zwnj_zwj_preserved=%d" % c["zwnj_zwj_preserved"])
    fp = sum(c["fold_punctuation"].values())
    if fp:
        bits.append("fold_punctuation=%d" % fp)
    fa = sum(c["fold_accents"].values())
    if fa:
        bits.append("fold_accents=%d" % fa)
    if c.get("whitespace_normalized"):
        bits.append("whitespace=%d" % c["whitespace_normalized"])
    lines.append("%s: %s%s" % (name, status, ("  (" + ", ".join(bits) + ")") if bits else ""))

    for ch in report["changes"]:
        loc = "%d:%d" % (ch["line"], ch["col"])
        if ch["action"] == "strip":
            lines.append("  %-9s %-16s strip %s %s" % (loc, ch["category"], ch["codepoint"], ch["name"]))
        else:
            lines.append("  %-9s %-16s %r -> %r" % (loc, ch["category"], ch["before"], ch["after"]))
    for w in report["warnings"]:
        lines.append("  warning %d:%d  %s" % (w["line"], w["col"], w["detail"]))
    cv = report.get("compile_verify") or {}
    if cv.get("attempted"):
        lines.append("  compile-verify: before_ok=%s after_ok=%s" % (cv["before_ok"], cv["after_ok"]))
    elif cv.get("reason") == "no_pdflatex":
        lines.append("  warning: pdflatex not found; compile-verify skipped "
                     "(structural guards still applied)")
    return lines


def aggregate(reports: list[dict]) -> dict:
    summary = {
        "files": len(reports),
        "cleaned": sum(1 for r in reports if r["status"] == "cleaned"),
        "unchanged": sum(1 for r in reports if r["status"] == "unchanged"),
        "refused": sum(1 for r in reports if r["status"] == "refused"),
        "bucket1_stripped": {},
        "zwnj_zwj_stripped": 0,
        "zwnj_zwj_preserved": 0,
        "fold_punctuation": {},
        "fold_accents": {},
        "whitespace_normalized": 0,
    }
    for r in reports:
        c = r.get("counts")
        if not c:
            continue
        for k in ("bucket1_stripped", "fold_punctuation", "fold_accents"):
            for cp, num in c.get(k, {}).items():
                summary[k][cp] = summary[k].get(cp, 0) + num
        for k in ("zwnj_zwj_stripped", "zwnj_zwj_preserved", "whitespace_normalized"):
            summary[k] += c.get(k, 0)
    return summary


# =============================================================================
# The pipeline -- the fail-closed spine.
# =============================================================================

def _blank_report(name: str) -> dict:
    return {
        "file": name,
        "status": "unchanged",
        "refusal": None,
        "counts": build_counts([], {}),
        "changes": [],
        "warnings": [],
        "compile_verify": {"attempted": False, "reason": "flag"},
    }


def process_text(text: str, name: str, is_tex: bool, opts: Options) -> tuple[str, dict]:
    """
    Steps 2-4 of the contract: compute everything, guard everything, return the
    would-be output. Writes nothing. Raises Refusal.
    """
    report = _blank_report(name)

    # --- lex ---------------------------------------------------------------
    forced_lexer = False
    if is_tex:
        try:
            lexres = lex(text)
        except LexRefusal as exc:
            if not opts.force_lexer:
                raise
            forced_lexer = True
            lexres = exc.partial
            report["warnings"].append({
                "line": LineIndex(text).linecol(exc.pos)[0],
                "col": LineIndex(text).linecol(exc.pos)[1],
                "detail": "--force-lexer: waived lexer refusal (%s); everything from this "
                          "point to end of file is treated as protected" % exc.detail,
            })
    else:
        lexres = lex_plain(text)
    report["warnings"].extend(lexres.warnings)

    # --- transform (edits only; no output string yet) ----------------------
    edits, warnings, extra = compute_edits(text, lexres, opts, is_tex)
    report["warnings"].extend(warnings)
    report["counts"] = build_counts(edits, extra)

    idx = LineIndex(text)
    rows = [edit_row(e, idx) for e in edits]
    report["changes"] = rows

    # --- the single place output is constructed ----------------------------
    out_text, out_ranges = apply_edits(text, edits)

    # --- guards ------------------------------------------------------------
    try:
        change_exactness_guard(text, out_text, rows)
    except Refusal:
        if not opts.force_change_exactness:
            raise
        report["warnings"].append({"line": 0, "col": 0, "detail":
                                   "--force-change-exactness: waived the guarantee that only the "
                                   "recorded codepoints at the recorded positions were changed"})

    if is_tex and edits:
        in_spans = [Span(s.kind, s.depth, s.start, s.end, text[s.start:s.end])
                    for s in lexres.spans]
        try:
            relex_guard(out_text, in_spans, out_ranges, edits, forced_lexer)
        except Refusal:
            if not opts.force_relex:
                raise
            report["warnings"].append({"line": 0, "col": 0, "detail":
                                       "--force-relex: waived the check that the output lexes to the "
                                       "same protected-region structure as the input"})

    report["status"] = "cleaned" if edits else "unchanged"
    return out_text, report


def process_file(path: str, opts: Options, write: bool, out_path: str | None = None) -> dict:
    """
    One file, end to end. Every failure -- guard trip or unexpected exception --
    resolves to a refusal with the original untouched, because nothing is written
    until the very last step and that step is atomic.
    """
    report = _blank_report(path)
    try:
        data = read_bytes(path)                                   # 1. read-only
        text, encoding, notes = decode_strict(data, opts.encoding, opts.force_encoding)

        is_tex = path.lower().endswith(TEX_SUFFIXES)
        out_text, report = process_text(text, path, is_tex, opts)  # 2-4. compute + guard
        for n in notes:
            report["warnings"].append({"line": 0, "col": 0, "detail": n})

        changed = out_text != text
        if is_tex and changed:
            report["compile_verify"] = compile_verify(path, text, out_text, opts.compile_verify)
        elif is_tex:
            report["compile_verify"] = {"attempted": False, "reason": "unchanged"}
        else:
            report["compile_verify"] = {"attempted": False, "reason": "not_latex"}

        if write and changed:
            target = out_path or path
            atomic_write(target, out_text, encoding, opts.backup and out_path is None)  # 5. atomic
        elif not write:
            # inspect: never writes. Section 8 gives it "unchanged" semantics --
            # the counts describe what is present, the file is not modified.
            report["status"] = "unchanged"
        return report

    except Refusal as exc:
        report["status"] = "refused"
        report["refusal"] = exc.as_dict()
        return report
    except Exception as exc:                                       # noqa: BLE001
        # An unexpected exception is a refusal, not a traceback over a half-written
        # file. Nothing has been written at this point by construction.
        report["status"] = "refused"
        report["refusal"] = {"guard": "internal", "line": None, "col": None,
                             "detail": "%s: %s" % (type(exc).__name__, exc)}
        return report


# =============================================================================
# Section 9 -- CLI.
# =============================================================================

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ERROR = 2

_BANNER = (
    "unicode_hygiene: normalizes invisible and format characters in text and LaTeX source. "
    "It does not detect or remove watermarks."
)


def _add_common(p: argparse.ArgumentParser, cleaning: bool) -> None:
    p.add_argument("files", nargs="*", help="files to process (omit to read stdin)")
    p.add_argument("--json", action="store_true", help="machine-readable report")
    if not cleaning:
        return
    p.add_argument("-o", dest="out", metavar="OUT", help="write here instead of in place (single input only)")
    p.add_argument("--fold-punctuation", action="store_true",
                   help="fold typographic punctuation to source form (off by default)")
    p.add_argument("--fold-accents", action="store_true",
                   help="fold accented letters to ASCII (off by default; warns on every fold)")
    p.add_argument("--all", action="store_true", help="enable both fold groups; disables no guard")
    p.add_argument("--encoding", metavar="NAME", help="explicit input encoding override")
    p.add_argument("--no-compile-verify", action="store_true", help="skip compile-verify entirely")
    p.add_argument("--compile-verify", action="store_true",
                   help="attempt compile-verify even for files judged to be fragments")
    p.add_argument("--backup", dest="backup", action="store_true", default=True,
                   help="keep a .bak of the original (default)")
    p.add_argument("--no-backup", dest="backup", action="store_false", help="do not write a .bak")
    p.add_argument("--force-relex", action="store_true",
                   help="WAIVES: the check that the output lexes to the same protected-region "
                        "structure as the input")
    p.add_argument("--force-change-exactness", action="store_true",
                   help="WAIVES: the guarantee that only the recorded codepoints at the recorded "
                        "positions were changed")
    p.add_argument("--force-encoding", action="store_true",
                   help="WAIVES: strict decoding; falls back to latin-1, which round-trips bytes "
                        "but guesses character identities")
    p.add_argument("--force-lexer", action="store_true",
                   help="WAIVES: lexer confidence refusals; the file is cleaned only up to the "
                        "point the lexer lost track, the remainder is left untouched")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="unicode_hygiene", description=_BANNER)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    _add_common(sub.add_parser("clean", help="normalize files in place (or to -o)"), cleaning=True)
    _add_common(sub.add_parser("inspect", help="report only; never writes"), cleaning=False)
    return p


def _options(args: argparse.Namespace) -> Options:
    fold_p = getattr(args, "fold_punctuation", False) or getattr(args, "all", False)
    fold_a = getattr(args, "fold_accents", False) or getattr(args, "all", False)
    mode = "auto"
    if getattr(args, "no_compile_verify", False):
        mode = "never"
    elif getattr(args, "compile_verify", False):
        mode = "always"
    return Options(
        fold_punctuation=fold_p,
        fold_accents=fold_a,
        encoding=getattr(args, "encoding", None),
        backup=getattr(args, "backup", True),
        compile_verify=mode,
        force_relex=getattr(args, "force_relex", False),
        force_change_exactness=getattr(args, "force_change_exactness", False),
        force_encoding=getattr(args, "force_encoding", False),
        force_lexer=getattr(args, "force_lexer", False),
    )


def _emit(reports: list[dict], as_json: bool, stream) -> None:
    if as_json:
        if len(reports) == 1:
            json.dump(reports[0], stream, ensure_ascii=False, indent=2)
        else:
            json.dump({"files": reports, "summary": aggregate(reports)},
                      stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    else:
        for r in reports:
            for line in render_human(r):
                print(line, file=stream)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    # Bare invocation with piped stdin is plain-text clean mode.
    if not argv and not sys.stdin.isatty():
        argv = ["clean"]
    if not argv:
        parser.print_help()
        return EXIT_ERROR

    args = parser.parse_args(argv)
    if args.cmd is None:
        parser.print_help()
        return EXIT_ERROR
    opts = _options(args)
    writing = args.cmd == "clean"
    if not writing:
        opts.compile_verify = "never"   # inspect is read-only and must cost nothing
    out_path = getattr(args, "out", None)

    if out_path and len(args.files) != 1:
        print("error: -o requires exactly one input file", file=sys.stderr)
        return EXIT_ERROR

    # ---- stdin -----------------------------------------------------------
    if not args.files:
        raw = sys.stdin.buffer.read()
        try:
            text, encoding, notes = decode_strict(raw, opts.encoding, opts.force_encoding)
        except Refusal as exc:
            print("refused <stdin>: %s: %s" % (exc.guard, exc.detail), file=sys.stderr)
            return EXIT_REFUSED
        try:
            out_text, report = process_text(text, "<stdin>", False, opts)
        except Refusal as exc:
            report = _blank_report("<stdin>")
            report["status"] = "refused"
            report["refusal"] = exc.as_dict()
            _emit([report], args.json, sys.stderr)
            return EXIT_REFUSED
        except Exception as exc:                                   # noqa: BLE001
            print("error <stdin>: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
            return EXIT_ERROR
        if not writing:
            report["status"] = "unchanged"
        else:
            sys.stdout.buffer.write(out_text.encode(encoding))
        report["compile_verify"] = {"attempted": False, "reason": "not_latex"}
        _emit([report], args.json, sys.stderr)
        return EXIT_OK

    # ---- files -----------------------------------------------------------
    reports: list[dict] = []
    errored = False
    for path in args.files:
        if not os.path.exists(path):
            r = _blank_report(path)
            r["status"] = "refused"
            r["refusal"] = {"guard": "io", "line": None, "col": None, "detail": "no such file"}
            reports.append(r)
            errored = True
            continue
        reports.append(process_file(path, opts, write=writing, out_path=out_path))

    _emit(reports, args.json, sys.stdout)
    if any(r["status"] == "refused" and r["refusal"]["guard"] == "internal" for r in reports):
        return EXIT_ERROR
    if errored:
        return EXIT_ERROR
    return EXIT_REFUSED if any(r["status"] == "refused" for r in reports) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
