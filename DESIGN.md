# Design — module layout + protected-region lexer state model

Status: **Part I is a historical record.** It was written as a pre-build proposal and
is kept because the reasoning still explains the shipped behaviour. Two things differ
from what was built: the code ships as a single file, `unicode_hygiene.py`, rather than
the package tree in §1, and the transforms described here exist and are covered by 89
tests. Part II documents the revision pipeline layered on top of it.

Environment checked: Python 3.13.7, pdfLaTeX 3.141592653-2.6-1.40.27 (TeX Live 2025) at `/Library/TeX/texbin/pdflatex`.

---

## 1. Module layout

Package name `unicode_hygiene` (importable, `python -m unicode_hygiene`). Stdlib only in the core; `subprocess` only inside `compile_verify.py`.

```
unicode_hygiene/
  __init__.py
  __main__.py          # python -m unicode_hygiene -> cli.main()
  errors.py            # Refusal(guard, line, col, detail), LexRefusal, UnexpectedError
  chars.py             # §3 tables ONLY — named frozensets/maps, zero logic
  latex_lexer.py       # §5.1 state machine -> LexResult | LexRefusal
  transform.py         # pure: (text, LexResult, Options) -> (edits, warnings). Never builds output.
  edits.py             # Edit dataclass + the single apply_edits() function
  guards.py            # §5.2 re-lex, §5.3 change-exactness, §5.4 decode, symlink check
  compile_verify.py    # §6, subprocess, cache, graceful absence
  safe_io.py           # §5.5 read-only read, temp+fsync+os.replace, .bak
  report.py            # §8 human + JSON rendering, aggregate mode
  pipeline.py          # the fail-closed spine (§2) — per-file orchestration
  cli.py               # argparse subcommands, stdin mode, exit codes
tests/
  test_lexer_regions.py   test_lexer_refusals.py  test_zwj_gating.py
  test_accent_gating.py   test_folds.py           test_plaintext.py
  test_failclosed.py      test_compile_verify.py  test_report.py
  test_idempotence.py
  fixtures/*.tex *.txt
  stubs/pdflatex          # executable stub for compile-verify tests
README.md
```

Dependency direction is strictly one-way, no cycles:

```
cli -> pipeline -> {safe_io, latex_lexer, transform, guards, compile_verify, report}
transform -> {chars, edits, latex_lexer(types only)}
guards -> {latex_lexer, edits, report}
everything -> errors
```

### The spine (`pipeline.py`) — §2 made structural

One function, `process_file(path, opts) -> FileResult`, wrapped in a single `try/except BaseException` that converts *anything* unexpected into `status="refused", guard="internal"`. Steps, in order; no write primitive exists above step 8:

1. `safe_io.read_bytes(path)` — opened `"rb"`, read-only, never reopened for writing.
2. `guards.decode_strict(bytes, opts.encoding)` — §5.4.
3. Lex (`.tex`) or synthesize an all-editable mask (`.txt`/stdin).
4. `transform` → `edits`, `warnings`, `changes` (report rows). **No output string yet.**
5. `apply_edits(text, edits)` → `out` (the only place output is constructed).
6. `guards.change_exactness(text, out, report_rows)` — §5.3.
7. `guards.relex(out, lex_in)` — §5.2.
8. `compile_verify` if applicable — §6.
9. Only now: `safe_io.atomic_write(path, out, backup=...)`.

`inspect` runs 1–7 and stops. This is why `inspect` "never writes" is a property of the call graph rather than an `if`.

### One deliberate anti-tautology choice (§5.3)

If the change-exactness guard replayed the same internal `Edit` objects the applier used, it would prove nothing. So: **the guard reconstructs the output from the serialized report rows** (`line`, `col`, `before`, `after` — the same fields the user and the JSON see), converting line/col back to offsets through an independently built line index, and compares byte-for-byte with `out`. A bug in the logger, the line/col arithmetic, or the transform all surface as a refusal. This is worth the redundancy; flag it if you disagree.

---

## 2. Protected-region lexer state model (§5.1)

### 2.1 Interface

```python
lex(text: str) -> LexResult          # raises LexRefusal
LexResult:
  protected:     bytearray  # len(text); 1 = no edit may touch this index
  accent_locked: bytearray  # len(text); 1 = accent folds forbidden here (bucket-1 still allowed)
  spans:         list[Span] # ordered, for the §5.2 re-lex comparison
  warnings:      list[Warning]
Span: (kind, depth, start, end)   # inclusive of the delimiters
```

Two masks rather than one, because the gates are different in kind: `protected` blocks *every* edit; `accent_locked` (§4 — `\author`, `\title`, `\thanks`, `\date`, `\cite*`, `\bibliography`, `\bibitem` line, `thebibliography`) blocks only accent folds. Stripping U+200B out of an author name is correct and desirable; folding `Erdős` there is the data loss we exist to prevent. `protected` implies `accent_locked`.

Per-character masks (O(n) bytes, ~20KB for a large paper) instead of interval search: cheaper to audit, impossible to get an off-by-one binary search wrong.

### 2.2 The state is a stack of frames, not an enum

`stack: list[Frame]`, bottom sentinel `DOC`. The **top frame selects the scanner mode**; the **whole stack decides protection**.

| Frame kind | Pushed by | Popped by | Protective |
|---|---|---|---|
| `DOC` | init | — | no |
| `COMMENT` | unescaped `%` | `\n` | **fold-lock only** (see 2.2a) |
| `VERB_INLINE(delim)` | `\verb`, `\verb*`, `\lstinline[..]`, `\mintinline{..}` | matching `delim` | yes |
| `VERBATIM_ENV(name)` | `\begin{verbatim|Verbatim|BVerbatim|lstlisting|minted|alltt|comment|listing}` | literal `\end{name}` | yes |
| `MATH_DOLLAR` | `$` | `$` | yes |
| `MATH_DDOLLAR` | `$$` | `$$` | yes |
| `MATH_PAREN` | `\(` | `\)` | yes |
| `MATH_BRACKET` | `\[` | `\]` | yes |
| `MATH_ENV(name)` | `\begin{equation|align|gather|multline|flalign|eqnarray|math|displaymath|array|cases|*matrix}` (+ starred) | `\end{name}` | yes |
| `GROUP(arg_of, argno)` | `{` | `}` | no |
| `ENV(name)` | any other `\begin{name}` | `\end{name}` | no |
| `BIBITEM_LINE` | `\bibitem` | `\n` | no (accent-locks) |

```
protected[i]     = any frame on stack is protective
fold_locked[i]   = protected[i] or COMMENT on stack
accent_locked[i] = fold_locked[i]
                 or any GROUP has arg_of ∈ ACCENT_LOCK_CMDS
                 or any ENV(thebibliography)
                 or BIBITEM_LINE on stack
```

### 2.2a Comments: three gates, not two (revised after review)

Comments are **not** protective. Bucket-1 and joiner stripping applies inside them
— the invisible characters go. Folds do not: a comment does not render, so a fold
there buys nothing, and rewriting a commented-out `\author{Erdős}` that someone
later uncomments is the same silent data loss §4 exists to prevent.

The COMMENT frame is still pushed and still selects the comment scanner mode, so a
stray `$`, `{`, or `\begin{verbatim}` inside a note cannot open a region for the rest
of the document. Its span is still emitted for the §5.2 re-lex — comment count,
ordering, and nesting depth must be unchanged — but the span's *text* is exempt from
the byte-equality check, since we now legitimately edit inside it. The content is
still fully covered by the §5.3 change-exactness guard, which compares the whole
output.

Because protection is "any frame on the stack," **nested math inside `\text{}` inside `align` is fully protected automatically** — we never re-enter text mode once a math frame is on the stack. That is the conservative branch the spec permits, and it costs us only the ability to clean stray U+200B inside `\text{}`, which is the right trade.

### 2.3 Scanner modes — the precedence that prevents corruption

At each position, mode comes from `stack[-1]`, checked in this order:

**Mode VERBATIM** (top is `VERB_INLINE` or `VERBATIM_ENV`) — *wins over everything.*
No backslash escaping. No `%` comments. No `$` math. Only the terminator is recognized:
- `VERB_INLINE`: the delimiter char. A `\n` or EOF first → **refuse** (`unterminated \verb`).
- `VERBATIM_ENV`: a literal `\end{name}` for *that exact name*. `\end{other}` is body text. `%` is body text. `\begin{...}` is body text.

**Mode COMMENT** (top is `COMMENT`) — consume to `\n`, no escape processing (a `\` at the end of a comment line does not escape the newline for our purposes; the comment ends at the newline).

**Mode NORMAL** — dispatch in this order:

1. `\` + next char:
   - **next is a letter** → scan control word `\[A-Za-z]+` plus an optional trailing `*`. Dispatch by name:
     - `verb`/`verb*` → read one delimiter char. Must be non-letter, non-`*`, non-newline, non-EOF, else **refuse**. Push `VERB_INLINE`.
     - `lstinline` → optional balanced `[...]`, then delimiter → push. `mintinline` → `{lang}`, then delimiter → push.
     - `begin` → skip spaces, read `{name}`, classify → push `VERBATIM_ENV` / `MATH_ENV` / `ENV`.
     - `end` → read `{name}`; must match the top `ENV`/`MATH_ENV`, else **refuse** (unbalanced).
     - name ∈ `ACCENT_LOCK_CMDS` → set `pending_arg_cmd = name` (see 2.4).
     - `bibitem` → push `BIBITEM_LINE`.
     - anything else → inert.
   - **next is non-letter** → a control symbol; consume both characters. `\(` `\)` `\[` `\]` push/pop the corresponding math frame. **Every other control symbol is inert literal** — this is what makes `\%` not a comment, `\$` not math, `\{` not a group, `\\` not an escape of the following char.
   - `\` at EOF → **refuse** (dangling backslash).
2. `%` → push `COMMENT`.
3. `$` → if top is `MATH_DOLLAR`, pop (single `$` closes; do not peek). If top is `MATH_DDOLLAR`, require `$$` to pop. Otherwise `$$` pushes `MATH_DDOLLAR`, single `$` pushes `MATH_DOLLAR`. A `$` while inside `MATH_ENV`/`MATH_PAREN`/`MATH_BRACKET` is ignored (illegal TeX, but the span is already protected, so ignoring cannot cause a wrong edit — refusing here would only produce false refusals).
4. `{` → push `GROUP`. `}` → pop if top is `GROUP`; if not, **warn** (see 2.5).
5. `\n` → pop `COMMENT`/`BIBITEM_LINE`; update the pending-argument state.

### 2.4 Accent-context tracking (§4), as lexer state not regex

A one-slot `pending_arg_cmd` register. Set when a control word in `ACCENT_LOCK_CMDS` is scanned. It survives spaces, a single newline, and balanced `[optional args]` (so `\cite[p.~3]{key}` works). The next `{` consumes it and stamps the new `GROUP` with `arg_of=name`; any other token clears it. `\thanks` inside `\author{...}` just nests two locked groups, which is already correct.

`ACCENT_LOCK_CMDS = {author, title, thanks, date, cite, citep, citet, citealp, citeauthor, citeyear, bibliography, bibitem}` — starred/`\Cite` variants folded in by normalizing the name.

### 2.5 Refusal vs warning — the line I propose

The lexer refuses **only when the unresolved ambiguity could change which regions are protected.** Everything else is a warning that leaves the document conservatively locked.

**Refuse** (specific diagnostic, `file: guard at line N col M: detail`):
- unterminated `\verb`/`\lstinline`/`\mintinline` (newline or EOF before the delimiter)
- `\verb` delimiter is a letter, `*`, newline, or EOF
- unterminated verbatim-family environment at EOF
- `\end{X}` mismatching the top `\begin{Y}`; `\end` with no open environment
- unclosed `\begin{...}` at EOF (spec-literal, §5.1)
- unterminated math at EOF (`$`, `$$`, `\(`, `\[`)
- dangling `\` at EOF

**Warn only:**
- unbalanced `}` with no open `GROUP`, and unclosed `{` at EOF. `GROUP` is non-protective; the only consequence is accent-context tracking, and an unclosed accent-locked group stays locked to EOF — the conservative direction. Refusing here would reject legitimate multi-file fragments for zero safety gain.

Per §7, `--force-lexer` exists and prints exactly what it waives ("protected-region detection may be wrong; edits may land inside verbatim/math") — because §7 requires every refusal be recoverable. It waives only analysis; the §2 write architecture stays.

### 2.6 What the re-lex guard compares (§5.2)

`spans` is an *ordered* list of `(kind, depth, start, end)` for protective frames only. The guard requires input and output to have the same length, same ordered `(kind, depth)` sequence, **and byte-identical span text**. The text comparison is stronger than the spec's count+depth requirement and is free, since by construction we never edit inside a protected span — if the text differs, something wrote where it must not.

---

## 3. Decisions I'd like confirmed before I build transforms

1. **`\lstinline{code}` / `\mintinline{py}{code}` brace form.** Real in the wild but not in §5.1. Proposal: accept `{` as a delimiter and match brace-balanced. Alternative is to refuse it as an unrecognized delimiter. I lean accept.
2. **U+201A / U+201E** (§3.3 says "make it a documented choice"). Proposal: `‚ → ,` and `„ → ,,` — matches the pdfLaTeX ligature convention and round-trips visually. Straight-quote alternative is available.
3. ~~**Comments are protected.**~~ **Resolved: comments are stripped but not folded.** See 2.2a.
4. **Warning-not-refusal for brace imbalance** (2.5). This is the only place I deviate from a literal reading of §5.1.
5. **Change-exactness reconstructs from the report, not internal edits** (§1). Costs a line-index round-trip; buys a non-tautological guard.

Say go (or amend) and I'll implement `chars.py` + `latex_lexer.py` + its test files first, then the transforms.

---
---

# Part II. The revision pipeline

Part I normalizes characters with a deterministic lexer. This layer rewrites prose with a
language model. The transform is therefore not verifiable by construction, so the design
puts its weight in two places: deciding what the model is allowed to see, and rejecting what
it returns.

Usage is in `README.md`. This section records why the parts have the shape they do. Each
rule below comes from a specific failure observed on a real manuscript.

## 4. Layout

```
revise_paper.py     orchestration: stage order, metrics, integrity report, exit code
  -> paper_vocab.py     masking, candidate selection, LLM calls, gates
  -> unicode_hygiene.py invoked as a subprocess, not imported
```

`unicode_hygiene.py` runs as a subprocess. It has its own fail-closed guards, exit codes, and
refusal semantics, described in Part I. Importing it would move its failures inside this
process, where we would have to reimplement its contract to honour them. A subprocess
boundary keeps that contract intact.

The three files stay separate. `paper_vocab.py` carries a 775KB embedded vocabulary and
`unicode_hygiene.py` carries its own lexer. One file would be about 850KB with three
argument parsers.

## 5. The masking model

Everything rests on this. Before any prose is touched, non-prose spans are replaced with
`\x00N\x00` placeholders and restored afterwards. NUL is used because the tokenizer's
`WORD_RE` requires letters, so a placeholder cannot be read as a word.

The round-trip is verified byte-identical on load. The check is cheap, and it is why the rest
of the system can be trusted. If masking is sound, the model cannot see a citation key, a
math span, or a package option.

The table below lists what is masked and the failure that put it there.

| Masked | Reason |
|---|---|
| the entire preamble | `\usetikzlibrary{positioning}` and `\definecolor{...}{RGB}{...}` put `positioning` and `RGB` into the prose stream as ordinary words. A substitution there breaks the build. |
| comments | See section 6. |
| math, verbatim, tikz, listings | Never prose. |
| `\cite`, `\ref`, `\label`, `\includegraphics` arguments | Keys, not words. |
| `FORMAL_ENVS`: `tlatex`, `notla`, `display`, `noj`, `conj`, `pf` | The `notla` environment from `tlatex.sty` holds raw TLA+, such as `Init == /\ x = None /\ turn = "input"`. Here `input` and `None` are identifiers. Rewriting one changes the specification, and the LaTeX still compiles, so nothing downstream reports it. |
| `tabular` grids and `\emph{}`-style commands with their arguments, in sentence modes | A rewritten sentence can introduce `&` and break a column count, or strand a brace. |

Two structural rules come from bugs.

Environment matching uses a backreference rather than a second alternation. The pattern
`\begin{(?:a|b)}.*?\end{(?:a|b)}` is non-greedy, so on nested environments
`\begin{display}...\begin{notla}...\end{notla}` closes at the wrong `\end` and leaves the
outer body exposed. The pattern in use is `\begin{(?P<env>...)}.*?\end{(?P=env)}`. Nesting an
environment inside another of the same name is still handled incorrectly. A real parser is
not worth the cost here, but the limit is real.

Extensibility is a flag rather than a patch. `--opaque-env` exists because formal-methods
papers invent environments, and freezing them should not require editing the tool.

## 6. Comments are a sentence boundary

A comment placeholder covers `%...` up to but not including its newline. A sentence spanning
one therefore has prose on both sides of a line break that only the comment terminates. When
a rewrite reorders text across it, prose that belonged on the next line lands on the comment
line and is commented out of the PDF.

This was observed once, on a TLA+ paper:

```
before: %% sm-2017-01-03: added to better reflect what StutterConstantCondition checks
after:  %% sm-2017-01-03: ... checks is contained in $\Sigma$. %% The sequence ...
```

68 of that paper's 785 sentences spanned a comment. The fix excludes them from candidacy
rather than trying to rewrite around the boundary.

This bug is the most instructive one in the project. Every structural gate passed. Brace
counts, citation keys, environment counts, and the compile were all correct. It was found by
diffing a category that should not have changed.

## 7. Two layers of gating

Mechanical gates run first, on every rewrite, before it is applied.

1. The placeholder multiset is preserved, so no citation or equation is dropped or
   duplicated.
2. No LaTeX metacharacters are introduced.
3. The result respects the concept ceiling, measured on the output. This catches a model that
   claims to have split a sentence and did not.
4. The length ratio is bounded.
5. For `decontrast`, the contrast detector finds nothing left.

These gates are necessary and not sufficient. All five are structural. The failures they
cannot detect are semantic. The following rewrite passed all of them:

> `The response is fluent, confident, and uniform.` / `Its three clauses are not.`
> becomes "Its three clauses express fluency, confidence, and uniformity."

That reverses the meaning. A second model call therefore re-reads each surviving rewrite
against its original and its preceding sentence. It is told to treat elided negation as a
reversal risk and to fail on doubt. On `decontrast` it rejected 5 of 6 rewrites. On
`simplify` it cut acceptance from 63 to 48.

Rejecting a good rewrite costs little. Accepting a wrong one in a submitted paper costs a
great deal. Both prompts state this, and the acceptance rates show it is believed.

## 8. Candidate selection

Three principles came out of measurement rather than design.

Frequency is the wrong selector for substitution. Ranking WordNet synonyms by corpus
frequency picks the most generic word available: `adjust` becomes `set`, `moves` becomes
`affect`, `routine` becomes `number`. Acceptance was 0.8%, and the rejections were correct.

Reachability is the right prune criterion. `suggest_synonyms` can only return a single-word
WordNet lemma, so the other 91% of the vocabulary is unreachable. Pruning to the reachable
set is lossless, verified by comparing every synonym returned before and after across probe
words. An "impact" ranking of the same size loses 24% of reachable synonyms, because it
spends its budget on bigrams.

Vagueness is what hurts a reader, not length. A Flesch-style objective rewards short common
words, which repeats the frequency failure in another form. Targeting a curated list of vague
words instead, and letting the model propose freely rather than choose from WordNet output,
moved acceptance from 0.8% to about 30% on that list. Replacements may be longer than what
they replace. A filter rejects any replacement that is itself in the vague list.

It follows that the corpus is useful only where the goal is register. For plain-language and
precision goals the corpus is academic prose and would pull the wrong way, so those
shortlists are corpus-independent by design.

## 9. Stage order

The order is `unicode`, `simplify`, `decontrast`, `precise`, `unicode`.

Structural rewrites run first, so `precise` operates on sentences that survive and
`decontrast` sees split sentences rather than packed ones.

The trailing `unicode` stage is required. Language models return typographic quotes. One
manuscript entered with 0 non-ASCII bytes and left the model stages with 8. Cleaning only at
the front leaves them in the submitted file.

Each stage writes to a fresh temporary file, so a failing stage leaves earlier work intact,
and `--tex` is refused as `--out`.

## 10. What the design does not cover

- Whether claims still match the data. The faithfulness check compares a rewrite to its
  original sentence, not to reality.
- Citation position within a paragraph. Keys and paragraph placement are checked. When a
  sentence splits, a citation attaches to one half, and that is not verified.
- Determinism. Two runs give different subsets, so yields are ranges.
- Environment nesting where both names are the same, as noted in section 5.
- Multi-file papers, which are handled by looping over files. Fragments report that the
  compile check does not apply rather than failing. The root document resolves `\input` via
  `TEXINPUTS` pointed at the source directory, with build products kept in a temporary
  directory.

All three bugs found in testing were caught by diffing a category that should not have
changed: spec environments, comments, and math multisets. None were caught by a structural
gate. Any category worth protecting is worth diffing.
