#!/usr/bin/env python3
"""
revise_paper.py: one command for the whole revision pipeline
===============================================================
Runs every stage over a LaTeX manuscript, in order, and writes ONE new file.
The source is never modified.

    python3 revise_paper.py --tex main.tex

STAGES (in order)
-----------------
  0. unicode     strip smart quotes, dashes, invisible characters   [unicode_hygiene.py]
  1. simplify    split sentences over the 3-concept ceiling; plainer wording
  2. decontrast  turn "not X but Y" framing into direct statements
  3. precise     replace vague words with ones that name what happens
  4. unicode     run again -- see below

Stage 4 is not redundant. Stages 1-3 send text through a language model, which
reliably returns typographic quotes and dashes: main.tex contained 0 non-ASCII
bytes and the model-revised output contained 8. Cleaning only at the front would
leave those in the file you submit.

WHY THIS IS AN ORCHESTRATOR, NOT ONE MERGED FILE
------------------------------------------------
paper_vocab.py carries a ~775KB embedded vocabulary and unicode_hygiene.py carries
its own LaTeX lexer. Pasting them together would produce an unmaintainable ~850KB
file with two argument parsers. This script is the single entry point; keep it in
the same directory as those two modules.

SAFETY
------
Every stage writes to a fresh temporary file, so a failing stage leaves the work so
far intact. At the end the LaTeX skeleton (citation keys, labels, refs, math
delimiters, braces, table separators) is compared against the original, and the
result is compiled if pdflatex is available. Both must pass for the run to be
reported as clean.

REQUIREMENTS
------------
    pip install nltk pdfplumber openai
    export OPENAI_API_KEY=...        (or put the key in a file named "conf" here)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    import paper_vocab as pv
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    sys.exit(f"Cannot import paper_vocab.py from {HERE}: {exc}")

UNICODE_TOOL = HERE / "unicode_hygiene.py"
LLM_STAGES = ("simplify", "decontrast", "precise", "register")
DEFAULT_STAGES = ("unicode", "simplify", "decontrast", "precise", "unicode")


def run_unicode_stage(src, dst, fold_accents=False):
    """Normalize typography via unicode_hygiene.py. Returns a count of edits."""
    if not UNICODE_TOOL.exists():
        print(f"  {UNICODE_TOOL.name} not found; skipping.", file=sys.stderr)
        shutil.copyfile(src, dst)
        return 0
    cmd = [sys.executable, str(UNICODE_TOOL), "clean", "--fold-punctuation",
           "--no-backup", "-o", str(dst), str(src)]
    if fold_accents:
        cmd.insert(4, "--fold-accents")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        print("  " + result.stdout.strip().replace("\n", "\n  "))
    if not Path(dst).exists():
        # "unchanged" means the tool wrote nothing; carry the text forward as-is.
        shutil.copyfile(src, dst)
    if result.returncode != 0:
        print(f"  unicode_hygiene exited {result.returncode}; text carried forward "
              "unchanged.", file=sys.stderr)
        print("  " + result.stderr.strip().replace("\n", "\n  "), file=sys.stderr)
    return len(re.findall(r"fold_\w+", result.stdout))


def run_llm_stage(src, dst, goal, args, report_path):
    """Run one paper_vocab revise goal. Returns the number of changes it applied."""
    pv.cmd_revise(argparse.Namespace(
        tex=str(src), out=str(dst), vocab=args.vocab, model=args.model, goal=goal,
        rare_max=args.rare_max, min_support=args.min_support,
        max_concepts=args.max_concepts, no_llm=args.no_llm, report=str(report_path),
        opaque_env=args.opaque_env,
    ))
    if not Path(dst).exists():
        shutil.copyfile(src, dst)
        return 0
    if Path(report_path).exists():
        try:
            return len(json.loads(Path(report_path).read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return 0


def latex_skeleton(text):
    """The structural features a rewrite must never disturb."""
    return {
        "environments": len(re.findall(r"\\begin\{", text)),
        "citation keys": sorted(re.findall(r"\\cite[tp]?\{([^}]*)\}", text)),
        "labels": sorted(re.findall(r"\\label\{([^}]*)\}", text)),
        "cross-refs": sorted(re.findall(r"\\(?:ref|autoref|cref)\{([^}]*)\}", text)),
        "math delimiters": text.count("$"),
        "braces": (text.count("{"), text.count("}")),
        "table separators": text.count("&"),
    }


def compile_check(tex_path, source_dir=None):
    """Compile twice (references need a second pass). Returns (ok, pages, errors).

    Returns (None, ...) when the check cannot be run: no pdflatex, or the file is an
    \\input fragment with no \\documentclass, which cannot compile on its own.
    """
    if not shutil.which("pdflatex"):
        return "no-pdflatex", None, None
    if "\\documentclass" not in tex_path.read_text(encoding="utf-8", errors="ignore"):
        return "fragment", None, None
    with tempfile.TemporaryDirectory(prefix="revise_compile_") as tmp:
        target = Path(tmp) / tex_path.name
        shutil.copyfile(tex_path, target)
        # A multi-file paper's root \inputs siblings and loads local .sty/.bib files.
        # Point TEXINPUTS/BIBINPUTS at the original directory so they resolve, while the
        # build products still land in the temp dir and never touch the source tree.
        env = dict(os.environ)
        if source_dir:
            for var in ("TEXINPUTS", "BIBINPUTS", "TEXMFHOME"):
                env[var] = f"{source_dir}:{env.get(var, '')}"
        for _ in range(2):
            subprocess.run([shutil.which("pdflatex"), "-interaction=nonstopmode",
                            target.name], cwd=tmp, capture_output=True, env=env)
        log = target.with_suffix(".log")
        pdf = target.with_suffix(".pdf")
        errors = len(re.findall(r"^!", log.read_text(errors="ignore"), re.M)) if log.exists() else -1
        # Page objects usually live inside compressed object streams, so counting them
        # in the raw bytes reports 0. Ask pdfinfo, and fall back to the log's shipout
        # markers rather than printing a page count that is silently wrong.
        pages = 0
        if pdf.exists():
            if shutil.which("pdfinfo"):
                info = subprocess.run([shutil.which("pdfinfo"), str(pdf)],
                                      capture_output=True, text=True)
                match = re.search(r"^Pages:\s*(\d+)", info.stdout, re.M)
                pages = int(match.group(1)) if match else 0
            if not pages and log.exists():
                pages = len(re.findall(r"\[\d+", log.read_text(errors="ignore")))
        return pdf.exists() and errors == 0, pages, errors


def text_metrics(text, extra_opaque=()):
    """Counts for the three things the pipeline is trying to reduce."""
    # Mask exactly what the stages mask, or the metrics describe a different document:
    # on a TLA+ paper the spec environments alone account for 2,700 phantom "sentences".
    masked, _ = pv.mask_latex(
        text, extra_envs=pv.SENTENCE_SAFE_ENVS + pv.FORMAL_ENVS + tuple(extra_opaque),
        extra_cmds=pv.SENTENCE_SAFE_CMDS)
    sentences = pv.split_sentences(masked)
    return {
        "sentences": len(sentences),
        "over ceiling": sum(1 for s in sentences if pv.count_concepts(s[2]) > 3),
        "contrastive": len(pv.shortlist_contrast(masked)),
        "vague words": len(pv.shortlist_precision(masked)),
        "non-ASCII": sum(1 for ch in text if ord(ch) > 127),
        "words": len(re.findall(r"[A-Za-z]+", re.sub(r"\x00\d+\x00", " ", masked))),
    }


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tex", required=True, help="Source .tex file (never modified)")
    parser.add_argument("--out", default=None,
                        help="Final output (default: <name>.final.tex)")
    parser.add_argument("--stages", default=",".join(DEFAULT_STAGES),
                        help=f"Comma-separated, in order (default: {','.join(DEFAULT_STAGES)})")
    parser.add_argument("--model", default=pv.DEFAULT_LLM_MODEL,
                        help=f"OpenAI model (default: {pv.DEFAULT_LLM_MODEL})")
    parser.add_argument("--vocab", default=None, help="Vocabulary for corpus-backed stages")
    parser.add_argument("--max-concepts", type=int, default=3,
                        help="Hard ceiling on claims per sentence (default: 3)")
    parser.add_argument("--rare-max", type=int, default=2, help="register stage threshold")
    parser.add_argument("--min-support", type=int, default=5, help="register stage support")
    parser.add_argument("--fold-accents", action="store_true",
                        help="Also fold accented characters in the unicode stages")
    parser.add_argument("--no-llm", action="store_true",
                        help="Dry run: report what each stage would target, change nothing")
    parser.add_argument("--keep-intermediates", action="store_true",
                        help="Keep each stage's output and JSON report")
    parser.add_argument("--opaque-env", default=None,
                        help="Comma-separated extra environment names to freeze entirely "
                             "(a paper's own spec/code environments)")
    parser.add_argument("--no-compile", action="store_true", help="Skip the pdflatex check")
    args = parser.parse_args()

    src = Path(args.tex)
    if not src.exists():
        sys.exit(f"No such file: {src}")
    out_path = Path(args.out) if args.out else src.with_suffix(".final.tex")
    if out_path.resolve() == src.resolve():
        sys.exit("Refusing to write over the source file; choose a different --out.")

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    unknown = [s for s in stages if s not in ("unicode",) + LLM_STAGES]
    if unknown:
        sys.exit(f"Unknown stage(s): {', '.join(unknown)}. "
                 f"Valid: unicode, {', '.join(LLM_STAGES)}")

    extra_opaque = tuple(e.strip() for e in (args.opaque_env or "").split(",") if e.strip())
    original = src.read_text(encoding="utf-8")
    before = text_metrics(original, extra_opaque)
    work = Path(tempfile.mkdtemp(prefix="revise_paper_"))
    current, tally = src, []

    for i, stage in enumerate(stages, 1):
        print(f"\n{'=' * 66}\nSTAGE {i}/{len(stages)}: {stage}\n{'=' * 66}")
        dst = work / f"stage{i}_{stage}.tex"
        if stage == "unicode":
            n = run_unicode_stage(current, dst, fold_accents=args.fold_accents)
        else:
            n = run_llm_stage(current, dst, stage, args, work / f"stage{i}_{stage}.json")
        tally.append((i, stage, n))
        current = dst

    final = current.read_text(encoding="utf-8")
    out_path.write_text(final, encoding="utf-8")
    after = text_metrics(final, extra_opaque)

    print(f"\n{'=' * 66}\nRESULT -> {out_path.name}   (source {src.name} untouched)\n{'=' * 66}")
    print("\nChanges per stage:")
    for i, stage, n in tally:
        print(f"  {i}. {stage:<12} {n:>4}")

    print("\nMetrics:")
    print(f"  {'':<16}{'before':>10}{'after':>10}")
    for key in before:
        flag = "" if before[key] == after[key] else "  <-"
        print(f"  {key:<16}{before[key]:>10}{after[key]:>10}{flag}")

    print("\nLaTeX integrity vs the original:")
    a, b = latex_skeleton(original), latex_skeleton(final)
    ok = True
    for key in a:
        same = a[key] == b[key]
        ok &= same
        shown = a[key] if not isinstance(a[key], list) else f"{len(a[key])}"
        print(f"  {'OK  ' if same else 'DIFF'} {key:<18} {shown}")
    if "\x00" in final:
        ok = False
        print("  DIFF mask placeholders leaked into the output")
    welded = len(re.findall(r"[a-z]\.[A-Z][a-z]", final))
    ok &= welded == 0
    print(f"  {'OK  ' if welded == 0 else 'DIFF'} {'welded sentences':<18} {welded}")

    if not args.no_compile:
        compiled, pages, errors = compile_check(out_path, source_dir=src.resolve().parent)
        if compiled == "no-pdflatex":
            print("\nCompile check skipped (pdflatex not on PATH).")
        elif compiled == "fragment":
            print("\nCompile check skipped: this file is an \\input fragment with no "
                  "\\documentclass. Compile the root document to verify.")
        else:
            ok &= compiled
            print(f"\nCompile: {'OK' if compiled else 'FAILED'} "
                  f"({pages} pages, {errors} errors)")

    if args.keep_intermediates:
        print(f"\nIntermediates: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + ("All checks passed. Review the diff before submitting."
                  if ok else "SOME CHECKS FAILED -- inspect before using this output."))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
