# preflight

Text hygiene and revision for plain text and LaTeX.

Two tools that compose. `unicode_hygiene.py` normalizes any text file — it strips
invisible and format characters, and folds typographic punctuation to source form when
asked. `revise_paper.py` layers a LaTeX manuscript revision pipeline on top of it, running
one command and writing one new file. Neither modifies the source.

```sh
cat notes.txt | python3 unicode_hygiene.py clean   # any text
python3 revise_paper.py --tex main.tex             # writes main.final.tex
```

- [Quick start](#quick-start)
- [The pipeline](#the-pipeline)
- [Safety model](#safety-model)
- [Building a vocabulary](#building-a-vocabulary)
- [Files](#files)
- [unicode_hygiene](#unicode_hygiene)

---

## Quick start

```sh
pip install nltk pdfplumber openai
python3 -c "import nltk; [nltk.download(p) for p in ('wordnet','stopwords','omw-1.4')]"
export OPENAI_API_KEY=sk-...        # or put the key in a file named "conf" here

python3 revise_paper.py --tex main.tex --no-llm   # dry run
python3 revise_paper.py --tex main.tex            # full run
```

If NLTK's downloader fails with an SSL certificate error, fetch the corpora with curl:

```sh
mkdir -p ~/nltk_data/corpora && cd ~/nltk_data/corpora
for p in stopwords wordnet omw-1.4; do
  curl -sSL -o $p.zip "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/corpora/$p.zip"
  unzip -oq $p.zip
done
```

`pdflatex` is optional. Without it the compile check is skipped and the structural checks
still run.

---

## The pipeline

There are five stages. Each stage reads the output of the previous one.

| # | Stage | What it changes |
|---|-------|-----------------|
| 0 | `unicode` | Smart quotes, dashes, and invisible characters become LaTeX source form |
| 1 | `simplify` | Splits sentences over the 3-concept ceiling and plains the wording |
| 2 | `decontrast` | Turns "not X but Y" framing into direct statements |
| 3 | `precise` | Replaces vague words with words that name what happens |
| 4 | `unicode` | Runs again |

Stage 4 is required. Stages 1 through 3 send text through a language model, and the model
returns typographic quotes. One manuscript went in with 0 non-ASCII bytes and came out of
the model stages with 8. A single pass at the front leaves those characters in the file.

The order is deliberate. Structural rewrites run first, so `precise` operates on the
sentences that survive and `decontrast` sees split sentences instead of packed ones.

### Measured results

On a 369-line, 6,409-word ACM manuscript:

| metric | before | after |
|--------|-------:|------:|
| sentences | 246 | 362 |
| over the 3-concept ceiling | 101 | 52 |
| contrastive constructions | 10 | 7 |
| non-ASCII bytes | 0 | 0 |
| words | 6,409 | 6,539 |

Document structure was unchanged: 8 sections, 11 subsections, 13 paragraphs, 17
environments, 7 captions, 26 labels, and 97 paragraph breaks, all identical and in the same
order. Of 51 citation keys, 0 changed paragraph.

On a 33,876-word TLA+ paper with 4,042 math delimiters, the same run cut sentences over the
ceiling from 223 to 146 and contrastive constructions from 48 to 19.

Acceptance rates vary by stage. `precise` accepts about 15%, `decontrast` about 10% to 50%
depending on the paper, and `simplify` produces most of the changes. The model is
nondeterministic, so two runs give different subsets.

### Options

```sh
python3 revise_paper.py --tex main.tex --stages unicode,simplify,unicode
python3 revise_paper.py --tex main.tex --keep-intermediates
```

Other flags are `--model` (default `gpt-5.6-luna`), `--max-concepts` (default 3), `--out`,
`--no-compile`, `--fold-accents`, `--no-llm`, and `--opaque-env`.

Note that `--no-llm` disables the language-model stages only. The unicode stages still run,
because they are deterministic. A dry run is byte-identical to the source only when the
source has no characters to fold.

Stages can also be run one at a time:

```sh
python3 paper_vocab.py revise --tex main.tex --goal simplify --out out.tex --report r.json
```

For a multi-file paper, loop over the files:

```sh
for f in content/*.tex; do
  python3 revise_paper.py --tex "$f" --out "revised/$(basename $f)"
done
```

Fragments with no `\documentclass` report that the compile check does not apply. Compile the
root document to verify them.

### Papers with their own spec or code environments

`--opaque-env` freezes named environments so nothing inside them is read as prose. On a TLA+
paper, the `notla` environment from `tlatex.sty` holds specification source:

```
Init == /\ x = None /\ turn = "input"
```

Here `input` and `None` are identifiers. A rewrite inside that environment changes the
specification, and the LaTeX still compiles, so nothing downstream reports a problem. The
environments shipped by common formal-methods packages are frozen by default: `tlatex`,
`notla`, `display`, `noj`, `conj`, `pf`, `lstlisting`, `alltt`, and others. Use
`--opaque-env` for a paper's own environments.

To see what a paper exposes before trusting a run:

```python
import paper_vocab as pv
t = open("paper.tex").read()
m, _ = pv.mask_latex(t, extra_envs=pv.SENTENCE_SAFE_ENVS + pv.FORMAL_ENVS,
                     extra_cmds=pv.SENTENCE_SAFE_CMDS)
print(sorted(set(pv.tokenize(m)))[:50])   # every word here is rewritable
```

---

## Safety model

Each stage writes to a fresh temporary file, so a failing stage leaves earlier work intact.
`revise_paper.py` refuses to write over its input.

Before any text is touched, non-prose spans are replaced with inert placeholders. This
covers the whole preamble, comments, math, verbatim and listing and tikz environments, and
the arguments of commands such as `\cite`, `\ref`, `\label`, and `\includegraphics`.
Sentence rewriting also freezes `tabular` grids, because a stray `&` breaks a column count,
and inline formatting commands together with their arguments. The masking round-trip is
verified byte-identical.

Every rewrite passes five mechanical gates before it is applied:

1. Placeholders survive intact, so no citation or equation is dropped or duplicated.
2. No LaTeX metacharacters are introduced.
3. The result respects the concept ceiling, measured on the output. This catches a model
   that claims to have split a sentence and did not.
4. The length ratio is bounded.
5. For `decontrast`, the contrast detector finds nothing left.

These gates are structural. The failures they cannot detect are semantic. The following
rewrite passed all five:

> `The response is fluent, confident, and uniform.` / `Its three clauses are not.`
> becomes "Its three clauses express fluency, confidence, and uniformity."

That reverses the meaning. A second model call therefore re-reads each surviving rewrite
against its original and the preceding sentence. It is told to treat elided negation as a
reversal risk and to fail on doubt. On `decontrast` it rejected 5 of 6 rewrites. On
`simplify` it cut acceptance from 63 to 48.

At the end of a run the tool compares the LaTeX skeleton against the original: environments,
citation keys, labels, cross-references, math delimiters, brace balance, and table
separators. It also checks for placeholder leakage and welded sentences such as `word.Next`,
and compiles the result with two `pdflatex` passes. The exit code is non-zero if any check
fails.

### What is not checked

- Whether the claims still match your data. The faithfulness check compares a rewrite to its
  original sentence, not to reality.
- Citation position within a paragraph. Keys and paragraph placement are verified. When a
  sentence splits, a citation attaches to one half, and that is not verified.
- Bibliography, figures, and table contents. These are frozen, not validated.
- Run-to-run consistency.

Read the diff before submitting. This is a copy-editing assistant. Sentences where a machine
rewrite is most likely to change your meaning are the sentences most worth writing by hand.

---

## Building a vocabulary

The `precise` and `register` stages rank word choices against a corpus of papers, so
suggestions reflect writing you have read. The shipped vocabulary was built from 12 papers
and is checked in. The source PDFs are not, so `suggest`, `top`, and `prune` work as shipped
and a rebuild needs your own corpus.

```sh
python3 paper_vocab.py build --pdf-dir /path/to/pdfs      # build from PDFs
python3 paper_vocab.py build --pdf-dir new/ --merge       # add papers, keep existing counts
python3 paper_vocab.py suggest --word utilize --llm       # corpus-ranked synonyms
python3 paper_vocab.py top --n 20 --min-docs 3            # most common terms
python3 paper_vocab.py prune --strategy suggestible       # shrink to reachable terms
```

`suggest --llm` combines two sources. The model proposes candidates and the corpus ranks
them and supplies the evidence, for example `use 334x in 12 papers`. Without `--llm` the
command uses WordNet and works offline.

Three properties of PDF extraction are handled automatically:

- pdfplumber's default `x_tolerance` of 3 glues words together on tightly set PDFs, giving
  tokens like `JournalofMachineLearningResearch`. On one paper 29% of tokens were unusable
  at the default and 0.3% at 1.5. Set it with `--x-tolerance`.
- `(cid:N)` glyph placeholders otherwise enter the vocabulary as the word `cid`, which became
  the most frequent term in the corpus. They are stripped at clean time and pruned at load
  for vocabularies built before the fix.
- Scanned PDFs with no text layer are skipped with a warning.

`prune --strategy suggestible` keeps single words present in WordNet. Those are the only
terms `suggest` can return, so the remaining 91% of the vocabulary is unreachable. Pruning
to them is lossless for synonym lookup, verified by comparing every synonym returned before
and after across a set of probe words. Keep the unpruned `vocab.full.json` for phrase lookups
and future merges.

---

## Files

| File | Role |
|------|------|
| `revise_paper.py` | Entry point for the pipeline |
| `paper_vocab.py` | Vocabulary building and the `revise` stages |
| `unicode_hygiene.py` | Unicode normalization, documented below |
| `test_unicode_hygiene.py` | Its test suite, 89 tests |
| `vocab.json` | Pruned vocabulary used for lookups |
| `vocab.full.json` | Unpruned corpus, the file to merge new papers into |
| `conf` | OpenAI API key, read only if `OPENAI_API_KEY` is unset. Gitignored; create your own |
| `DESIGN.md` | Why the pipeline is shaped this way, and the bugs that shaped it |

`revise_paper.py` imports the other two modules, so keep all three in one directory. They
are separate files because `paper_vocab.py` carries a 775KB embedded vocabulary and
`unicode_hygiene.py` carries its own LaTeX lexer. One merged file would be about 850KB with
three argument parsers.

---

# unicode_hygiene

A fail-closed Unicode hygiene tool for plain-text and LaTeX manuscripts. Used as stages 0
and 4 above, and usable on its own.

AI-assisted drafting, web copy-paste, and round-trips through word processors leave
invisible and zero-information format characters in source files. In `.tex` they
survive into camera-ready submissions, where they cause compile warnings, PDF/A
validation failures, and silent glyph problems. This tool removes them, and
optionally normalizes typographic punctuation and accented letters to their LaTeX
source forms.

**What this is.** A text-hygiene tool. It normalizes text: it strips invisible and
format characters, and folds typographic characters to source form when asked.

**What this is not.** It does not detect, identify, or score watermarks, and it
makes no claim to defeat one. It has no network access and no telemetry. It
handles plain text and LaTeX only, and never rewrites or paraphrases prose.

**What follows from the normalization.** Bucket 1 strips the invisible-character
surface unconditionally — zero-width characters, bidi controls, the Unicode TAG
block, the variation selectors — because those characters cause compile warnings
and PDF/A validation failures. Anything encoded in those codepoints goes with
them, whatever it was put there for. The tool does not look for such an encoding,
cannot tell you whether one was present, and reports only the characters it
removed. What it does not touch: substitutions that use visible characters, such
as Cyrillic а for Latin a, and anything carried in word choice rather than in
bytes.

### Install

Nothing to install. One file, Python 3.10+, standard library only.

```sh
python3 unicode_hygiene.py --help
```

`pdflatex` is used for the optional compile-verify step (below). If it is absent,
that step is skipped with a warning and the structural guards carry the safety on
their own.

### Use

```sh
# Report what is present. Never writes.
python3 unicode_hygiene.py inspect paper.tex

# Strip invisible characters in place, keeping paper.tex.bak
python3 unicode_hygiene.py clean paper.tex

# Also fold typographic punctuation and accents (both off by default)
python3 unicode_hygiene.py clean --all paper.tex

# Characterize a whole corpus in one run
python3 unicode_hygiene.py inspect --json chapters/*.tex

# Plain text over a pipe
cat notes.txt | python3 unicode_hygiene.py clean > notes.clean.txt
```

#### Commands

```
clean   <file>... [-o OUT] [--fold-punctuation] [--fold-accents] [--all]
                  [--json] [--encoding NAME] [--no-compile-verify] [--compile-verify]
                  [--force-relex] [--force-change-exactness] [--force-encoding]
                  [--force-lexer] [--backup | --no-backup]
inspect <file>... [--json]        report only, never writes
        (stdin)                   plain-text mode when no file is given
```

Multiple files are a batch; each file succeeds or is refused independently, and one
refusal does not abort the rest. `-o` takes a single input; batch runs write in
place with a `.bak` unless `--no-backup`.

**Exit codes.** `0` — every file was cleaned or was already clean. `1` — at least
one file was refused by a guard. `2` — an unexpected error or a bad invocation.
Refusal and unexpected error are distinct code paths, not just distinct numbers.

Refusals print as `refused <file>: <guard> at line <N>: <detail>`.

### The safety model

> Break the tool, never the file.

Every failure mode resolves to the same outcome: **original untouched, nothing
written, non-zero exit, a specific diagnostic naming the file, the guard, and the
line.**

This is structural, not a matter of discipline. `process_file()` runs:

1. read the input read-only into memory;
2. compute the entire cleaned result and change report in memory, as an *edit list* —
   the transform never builds an output string itself;
3. `apply_edits()`, the single place an output string is constructed;
4. run every guard against the in-memory result;
5. only then write: temp file in the destination directory → `fsync` → atomic
   `os.replace`. A `.bak` of an existing target goes through the same safe path first.

No write primitive is reachable before step 5, so an exception anywhere in steps 1–4
is *incapable* of leaving a partial file. An unexpected exception becomes a refusal,
not a traceback over a half-written file. The original is never opened for writing.

#### Guards (not toggleable, all fail-closed)

**Protected-region lexer.** A state machine — not a full LaTeX parser — that tracks
which regions the cursor is in so no edit is ever made inside one: inline verbatim
(`\verb`, `\verb*`, `\lstinline`, `\mintinline`, any delimiter), the verbatim
environment family, and every form of math mode. Comments are tracked too, but as a
weaker gate — see "deliberate choices". Escaped specials (`\%`,
`\$`, `\{`, …) are literals and never open or close a region. If the lexer reaches a
state it cannot resolve — an unterminated `\verb`, unbalanced `\begin`/`\end`, an
unterminated math region — it **refuses the file** rather than guessing.

**Output re-lex.** The cleaned result is lexed again, and the protected-region
structure must be identical to the input's: same regions, same nesting depth, same
byte content. An edit that changed how the document lexes is a corruption signal.

**Change-exactness.** The output is reconstructed from the *serialized change
report* — the same `line`/`col`/`before`/`after` rows you and `--json` see — and must
equal the actual output byte for byte. Reconstructing from the report rather than
from the internal edit objects is deliberate: replaying the objects the applier
already consumed would prove nothing. Any bug in the transform, the logger, or the
column arithmetic surfaces as a refusal instead of a silent unintended edit.

**Encoding.** Input is decoded strictly as UTF-8, falling back to a BOM-declared
encoding. Never `errors='replace'` or `errors='ignore'` — forcing a decode can
itself corrupt. Use `--encoding NAME` to override.

**Write safety.** Temp file + `fsync` + atomic `os.replace`. A symlink destination
is refused. The original is opened read-only.

#### Escape hatches

Each `--force-*` flag prints exactly which safety it waives, and all are off by
default. A refused file is therefore always recoverable by a user who has inspected
it — a false refusal never permanently blocks anyone.

| Flag | Waives |
|---|---|
| `--force-lexer` | Lexer confidence refusals. The file is cleaned only up to the point the lexer lost track; everything after it is left untouched. |
| `--force-relex` | The check that the output lexes to the same protected-region structure as the input. |
| `--force-change-exactness` | The guarantee that only the recorded codepoints at the recorded positions were changed. |
| `--force-encoding` | Strict decoding. Falls back to latin-1, which round-trips bytes exactly but guesses character identities. |

**No force flag waives the write architecture.** In-memory computation, the atomic
write, and the read-only original hold unconditionally. Only the analysis guards can
be waived.

### What gets changed

#### Always: invisible and format characters

Zero-width space, word joiner, the invisible math operators, BOM, soft hyphen,
combining grapheme joiner, Mongolian vowel separator, the LTR/RTL marks, the bidi
embeddings, overrides and isolates, the entire Unicode TAG block, and both variation
selector blocks. These are enumerated explicitly rather than derived from Unicode
general categories, which both over- and under-select for this task.

ZWNJ (U+200C) and ZWJ (U+200D) are **context-gated**, because they are required in
some scripts and in emoji sequences. One is stripped only when neither neighbour
— skipping other invisible characters in between — is Arabic, Indic, or pictographic.
When adjacency is ambiguous, such as at a protected-region boundary, the joiner is
**preserved**. That is the safe direction.

#### Opt-in: `--fold-punctuation`

Typographic characters to LaTeX source form: smart quotes to `` ` ``/`'`/```` `` ````/`''`,
en and em dashes to `--`/`---`, ellipsis to `\ldots{}`, no-break space to `~`, thin
space to `\,`, and the other Unicode spaces to a regular space.

U+2212 MINUS and U+00D7 MULTIPLICATION fold to `$-$` and `$\times$`. Note the
interaction with math mode: an occurrence already inside math lives in a protected
region and is never examined, so only stray *text-mode* occurrences are ever folded.

Documented choices: U+201A folds to `,` and U+201E to `,,`, matching the pdfLaTeX
ligature convention. In plain text there is no LaTeX to emit, so the same source
characters fold to their conventional ASCII renderings instead (`—` → `--`,
`“…”` → `"…"`, `…` → `...`, `×` → `x`).

#### Opt-in and heavily gated: `--fold-accents`

Under pdfLaTeX with `inputenc`, an `é` in the source is **intended content**, not
junk. So even with the flag explicitly passed:

- every fold emits a warning, recorded in the report;
- folds never happen inside `\author{}`, `\title{}`, `\thanks{}`, `\date{}`, inside a
  `thebibliography` environment or on a `\bibitem` line, or inside the argument of
  `\cite`/`\citep`/`\citet`/`\bibliography` and their relatives.

Folding `Erdős` → `Erdos` in an author list or a citation key is silent data loss,
and is precisely the failure this tool must not produce. These contexts are tracked
in the lexer's state, not by a regex.

The gate applies to accent folds only. Invisible characters are still stripped from
an author name, which is correct and desirable.

#### Plain text only: whitespace

For `.txt` and stdin, where no protected regions exist: runs of spaces and tabs
collapse to a single space, CRLF/CR become LF, trailing whitespace is stripped per
line, and runs of 3 or more blank lines collapse to 2. These are on by default for
plain text and **never** run on `.tex`, where whitespace is semantic.

### Compile-verify

For a standalone `.tex` document — one containing `\documentclass` with no unresolved
`\input`/`\include` before it — that was actually edited, both the original and the
cleaned version are compiled in a scratch directory, from the file's own directory so
relative `\input`, graphics, and `.bib` paths resolve. If the original compiled and
the cleaned version does not, **the change is refused.** If the original did not
compile either, that is reported and not held against the edit.

Fragments and subfiles legitimately do not compile alone and are never refused for
it — compile-verify is skipped for them entirely. Unedited files skip compilation, and
the "before" outcome is cached. `--no-compile-verify` force-skips; `--compile-verify`
force-attempts on a file heuristically judged a fragment.

If `pdflatex` is missing, the step is skipped with a warning. The structural guards
are sufficient for safety on their own.

### The change report

Always emitted; it is both the audit trail and the measurement instrument.

Human form gives a summary line with per-category counts, then one line per change
with `line:col`, category, and either the codepoint and its Unicode name or the
before → after of a fold. Warnings, including every accent fold, are marked.

```
paper.tex: cleaned  (bucket1=6, fold_punctuation=8)
  4:15      bucket1          strip U+200B ZERO WIDTH SPACE
  8:49      fold_punctuation '—' -> '---'
  9:34      fold_punctuation '−' -> '$-$'
  warning 5:17  accent fold ő->o skipped: \author{...} context
  compile-verify: before_ok=True after_ok=True
```

`--json` gives one object per file with `status`, `refusal`, `counts`, `changes`,
`warnings`, and `compile_verify`. Over several files it emits `{"files": [...],
"summary": {...}}`, so a corpus can be characterized in one run.

`inspect` produces the same report and never writes, which makes it the mode for
measuring contamination across a corpus. It reports `status: "unchanged"` because
nothing on disk changed; the counts still describe everything present.

### Notes and deliberate choices

- **Comments are cleaned, but never folded.** Invisible characters inside a `%`
  comment are stripped like any others. Folds are not applied there: a comment does
  not render, so folding buys nothing, and rewriting a commented-out `\author{Erdős}`
  that someone later uncomments is the same silent data loss §4 exists to prevent.
  Comments are still *lexed* as comments, so a stray `$` or `{` in a note cannot open
  math or a group for the rest of the document.
- **Variation selectors are stripped**, U+FE0F included, per the character table.
  They are still honoured when *classifying* a joiner's neighbour, so an emoji ZWJ
  sequence keeps its ZWJ.
- **Brace imbalance is a warning, not a refusal.** A brace group is not a protective
  region, so an unbalanced `}` cannot change what is protected; an unclosed
  accent-locked group simply stays locked to end of file, the conservative direction.
  Refusing would reject legitimate multi-file fragments for no safety gain.
  `\begin`/`\end` imbalance *is* a refusal.
- **Once inside math, the lexer never re-enters text mode.** Nested `$…$` inside
  `\text{}` inside `align` is therefore fully protected. That is the safe branch, and
  it costs only the ability to clean inside `\text{}`.
- **Cleaning is idempotent.** The output of a clean is a fixed point; running twice
  changes nothing the second time. This is tested.

### Files

| | |
|---|---|
| `unicode_hygiene.py` | the tool — everything, standard library only |
| `test_unicode_hygiene.py` | the test suite (85 tests) |
| `DESIGN.md` | module layout and the lexer state model |

```sh
python3 -m unittest -v test_unicode_hygiene
```