#!/usr/bin/env python3
"""pdf2tex — convert PDF to compilable LaTeX via MinerU + pandoc."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
#  LaTeX preamble injected into every output .tex
# ---------------------------------------------------------------------------

LATEX_PREAMBLE = r"""\usepackage[T2A]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage[russian]{babel}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{amsfonts}
\usepackage{mathtools}
\usepackage{upgreek}
\usepackage{graphicx}
\usepackage[margin=2.5cm]{geometry}
\usepackage{array}
\usepackage{booktabs}
\usepackage{longtable}

% --- fallback stubs for common OCR junk commands ---
\providecommand{\varnothing}{\emptyset}
\providecommand{\operatorname}[1]{\mathrm{#1}}
\providecommand{\lvert}{\vert}
\providecommand{\rvert}{\vert}
\providecommand{\lVert}{\Vert}
\providecommand{\rVert}{\Vert}"""

# ---------------------------------------------------------------------------
#  Greek command names (used to split merged tokens like \kappaM)
# ---------------------------------------------------------------------------

GREEK_COMMANDS = {
    "alpha",
    "beta",
    "gamma",
    "delta",
    "epsilon",
    "varepsilon",
    "zeta",
    "eta",
    "theta",
    "vartheta",
    "iota",
    "kappa",
    "lambda",
    "mu",
    "nu",
    "xi",
    "pi",
    "varpi",
    "rho",
    "varrho",
    "sigma",
    "varsigma",
    "tau",
    "upsilon",
    "phi",
    "varphi",
    "chi",
    "psi",
    "omega",
    "Gamma",
    "Delta",
    "Theta",
    "Lambda",
    "Xi",
    "Pi",
    "Sigma",
    "Upsilon",
    "Phi",
    "Psi",
    "Omega",
    # upgreek
    "upalpha",
    "upbeta",
    "upgamma",
    "updelta",
    "upepsilon",
    "upzeta",
    "upeta",
    "uptheta",
    "upiota",
    "upkappa",
    "uplambda",
    "upmu",
    "upnu",
    "upxi",
    "uppi",
    "uprho",
    "upsigma",
    "uptau",
    "upupsilon",
    "upphi",
    "upchi",
    "uppsi",
    "upomega",
}

_GREEK_SORTED = sorted(GREEK_COMMANDS, key=len, reverse=True)
_GREEK_RE = re.compile(
    r"\\(" + "|".join(re.escape(g) for g in _GREEK_SORTED) + r")([A-Za-z0-9])"
)

# ---------------------------------------------------------------------------
#  PUA (Private Use Area) U+F0xx → ASCII  (PDF Symbol font encoding)
# ---------------------------------------------------------------------------

PUA_MAP: dict[str, str] = {chr(0xF000 + c): chr(c) for c in range(0x20, 0x7F)}

# Extended PUA mappings beyond ASCII (Symbol font encoding)
PUA_MAP_EXT: dict[str, str] = {
    "\uF0B7": "\\textbullet{}",   # bullet
    "\uF0D7": "$\\times$",        # multiplication sign
    "\uF0F7": "$\\div$",          # division sign
    "\uF0AE": "$\\Rightarrow$",   # right double arrow
    "\uF0A7": "\\S{}",            # section sign
    "\uF0B0": "$^{\\circ}$",      # degree sign
    "\uF0B1": "$\\pm$",           # plus-minus
    "\uF0AB": "$\\leftarrow$",    # left arrow
    "\uF0AC": "$\\uparrow$",      # up arrow
    "\uF0AD": "$\\downarrow$",    # down arrow
    "\uF0BB": "$\\equiv$",        # equivalence
    "\uF0B2": "$\\geq$",          # greater or equal
    "\uF0A3": "$\\leq$",          # less or equal
    "\uF0E4": "$\\otimes$",       # circled times
    "\uF0C5": "$\\|$",            # double vertical bar
}


# ---------------------------------------------------------------------------
#  Bare Unicode Greek letters → LaTeX commands  (for pdflatex + T2A)
# ---------------------------------------------------------------------------

BARE_GREEK_MAP: dict[str, str] = {
    "Α": r"A", "Β": r"B", "Ε": r"E", "Ζ": r"Z",  # visually identical to Latin
    "Η": r"H", "Ι": r"I", "Κ": r"K", "Μ": r"M",
    "Ν": r"N", "Ο": r"O", "Ρ": r"P", "Τ": r"T",
    "Υ": r"Y", "Χ": r"X",
    "Γ": r"$\Gamma$", "Δ": r"$\Delta$", "Θ": r"$\Theta$",
    "Λ": r"$\Lambda$", "Ξ": r"$\Xi$", "Π": r"$\Pi$",
    "Σ": r"$\Sigma$", "Φ": r"$\Phi$", "Ψ": r"$\Psi$",
    "Ω": r"$\Omega$",
    "α": r"$\alpha$", "β": r"$\beta$", "γ": r"$\gamma$",
    "δ": r"$\delta$", "ε": r"$\varepsilon$", "ζ": r"$\zeta$",
    "η": r"$\eta$", "θ": r"$\theta$", "ι": r"$\iota$",
    "κ": r"$\kappa$", "λ": r"$\lambda$", "μ": r"$\mu$",
    "ν": r"$\nu$", "ξ": r"$\xi$", "π": r"$\pi$",
    "ρ": r"$\rho$", "σ": r"$\sigma$", "ς": r"$\sigma$",
    "τ": r"$\tau$", "υ": r"$\upsilon$", "φ": r"$\varphi$",
    "χ": r"$\chi$", "ψ": r"$\psi$", "ω": r"$\omega$",
}
# ---------------------------------------------------------------------------
#  Dehyphenation patterns
# ---------------------------------------------------------------------------

_LETTER = r"[а-яА-ЯёЁa-zA-Z]"
_LOWER = r"[а-яёa-z]"


# ===================================================================
#  Pipeline helpers
# ===================================================================


def dehyphenate(text: str) -> str:
    """Join soft hyphens that PDF line-breaks introduced.

    Merges 'рассеи-\\nвающим' → 'рассеивающим' while leaving real
    hyphens ('какой-либо') and capitalized continuations alone.
    Skips display-math blocks ($$...$$).
    """
    lines = text.split("\n")
    out: list[str] = []
    in_math = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("$$"):
            in_math = not in_math
            out.append(line)
            i += 1
            continue
        if in_math:
            out.append(line)
            i += 1
            continue
        if (
            i + 1 < len(lines)
            and re.search(rf"{_LETTER}-\s*$", line)
            and re.match(rf"\s*{_LOWER}", lines[i + 1])
        ):
            out.append(re.sub(r"-\s*$", "", line) + lines[i + 1].lstrip())
            i += 2
        else:
            out.append(line)
            i += 1
    return "\n".join(out)


def _split_greek(m: re.Match[str]) -> str:
    return f"\\{m.group(1)} {m.group(2)}"


# ===================================================================
#  Step 1 — MinerU  (PDF → Markdown)
# ===================================================================


def run_mineru(pdf_path: Path, work_dir: Path) -> Path:
    """Run magic-pdf on a PDF, return path to output .md file."""
    print(f"[1/4] MinerU  {pdf_path.name}")
    cmd = [
        "magic-pdf",
        "-p",
        str(pdf_path),
        "-o",
        str(work_dir),
        "-m",
        "auto",
    ]
    env = os.environ.copy()
    env.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        print("  ✗ MinerU failed", file=sys.stderr)
        sys.exit(1)

    stem = pdf_path.stem
    for sub in ("auto", "ocr", "txt"):
        p = work_dir / stem / sub / f"{stem}.md"
        if p.exists():
            print(f"      {p}")
            return p

    found = list(work_dir.rglob("*.md"))
    if found:
        best = max(found, key=lambda f: f.stat().st_size)
        print(f"      {best}")
        return best

    print("  ✗ no .md produced", file=sys.stderr)
    sys.exit(1)


# ===================================================================
#  Step 2 — clean Markdown (dehyphenation, PUA, merged commands)
# ===================================================================


def clean_markdown(md_path: Path) -> Path:
    """In-place cleanup of MinerU's Markdown output."""
    print("[2/4] clean md")
    text = md_path.read_text("utf-8")
    tags: list[str] = []

    # PUA → ASCII / LaTeX
    pua_n = 0
    for mapping in (PUA_MAP, PUA_MAP_EXT):
        for k, v in mapping.items():
            n = text.count(k)
            if n:
                text = text.replace(k, v)
                pua_n += n
    if pua_n:
        tags.append(f"pua={pua_n}")

    # dehyphenation
    before = len(text)
    text = dehyphenate(text)
    diff = before - len(text)
    if diff:
        tags.append(f"dehyphen={diff}")

    # split merged greek: \kappaM → \kappa M
    text, n = _GREEK_RE.subn(_split_greek, text)
    if n:
        tags.append(f"greek_split={n}")

    # bare Unicode Greek → LaTeX
    greek_n = 0
    for k, v in BARE_GREEK_MAP.items():
        n = text.count(k)
        if n:
            text = text.replace(k, v)
            greek_n += n
    if greek_n:
        tags.append(f"bare_greek={greek_n}")

    # fullwidth / misc Unicode -> ASCII/LaTeX
    _fw_map = {
        "\uff0d": "-",       # fullwidth hyphen-minus
        "\uff0b": "+",       # fullwidth plus
        "\uff08": "(",       # fullwidth left paren
        "\uff09": ")",       # fullwidth right paren
        "\uff1d": "=",       # fullwidth equals
        "\u00b2": "$^{2}$",  # superscript 2
        "\u00b3": "$^{3}$",  # superscript 3
        "\u00b9": "$^{1}$",  # superscript 1
    }
    fw_n = 0
    for k, v in _fw_map.items():
        n = text.count(k)
        if n:
            text = text.replace(k, v)
            fw_n += n
    if fw_n:
        tags.append(f"fullwidth={fw_n}")

    # junk \?
    n = text.count("\\?")
    if n:
        text = text.replace("\\?", "")
        tags.append(f"junk_qmark={n}")

    md_path.write_text(text, "utf-8")
    if tags:
        print(f"      {', '.join(tags)}")
    return md_path


# ===================================================================
#  Step 3 — pandoc  (Markdown → LaTeX)
# ===================================================================


def md_to_tex(md_path: Path, tex_path: Path) -> None:
    """Convert Markdown to standalone .tex via pandoc."""
    print("[3/4] pandoc")
    preamble = md_path.parent / "_preamble.tex"
    preamble.write_text(LATEX_PREAMBLE, "utf-8")
    cmd = [
        "pandoc",
        str(md_path),
        "-o",
        str(tex_path),
        "--standalone",
        "--from=markdown+tex_math_dollars+tex_math_single_backslash",
        "--to=latex",
        f"--include-in-header={preamble}",
        "-V",
        "documentclass=article",
        "-V",
        "classoption=12pt,a4paper",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"  ✗ pandoc:\n{proc.stderr}", file=sys.stderr)
        sys.exit(1)
    warns = [w for w in proc.stderr.strip().splitlines() if w]
    if warns:
        print(f"      {len(warns)} warning(s)")
    print(f"      {tex_path}")


# ===================================================================
#  Step 4 — post-process .tex
# ===================================================================


def _fix_delimiters(content: str) -> tuple[str, int]:
    """Balance \\left / \\right inside math environments."""
    count = 0
    left_re = re.compile(r"\\left\s*(?:[\[\(\{|./]|\\[{}]|\\l[vV]ert)")
    right_re = re.compile(r"\\right\s*(?:[\]\)\}|./]|\\[{}]|\\[rl][vV]ert)")

    def _bal(m: re.Match[str]) -> str:
        nonlocal count
        f = m.group(0)
        nl, nr = len(left_re.findall(f)), len(right_re.findall(f))
        if nl == nr:
            return f
        count += 1
        if nr > nl:
            for _ in range(nr - nl):
                f = re.sub(
                    r"\\right\s*(?:([)\]}|.])|\\[rl][vV]ert)",
                    lambda x: x.group(1) or "|",
                    f,
                    count=1,
                )
        else:
            for _ in range(nl - nr):
                f = re.sub(
                    r"\\left\s*(?:([(\[{|.])|\\l[vV]ert)",
                    lambda x: x.group(1) or "|",
                    f,
                    count=1,
                )
        return f

    for pat in (
        r"\$\$.*?\$\$",
        r"\\\[.*?\\\]",
        r"\\\(.*?\\\)",
        r"(?<!\$)\$(?!\$).+?(?<!\$)\$(?!\$)",
        (
            r"\\begin\{(equation|align|gather|multline)\*?\}.*?"
            r"\\end\{(equation|align|gather|multline)\*?\}"
        ),
    ):
        content = re.sub(pat, _bal, content, flags=re.DOTALL)
    return content, count


def _fix_alignment_tabs(content: str) -> tuple[str, int]:
    """Trim extra & in tabular/array rows beyond column spec width."""
    count = 0
    pat = re.compile(
        r"(\\begin\{(?:tabular|array)\}\{([^}]*)\})(.*?)(\\end\{(?:tabular|array)\})",
        re.DOTALL,
    )

    def _fix(m: re.Match[str]) -> str:
        nonlocal count
        begin, spec, body, end = m.group(1), m.group(2), m.group(3), m.group(4)
        ncols = len(re.findall(r"[lcrpXm]", spec))
        if ncols == 0:
            return m.group(0)
        maxtabs = ncols - 1
        rows = []
        for row in body.split(r"\\"):
            if row.count("&") > maxtabs:
                count += 1
                parts = row.split("&")
                row = "&".join(parts[: maxtabs + 1])
            rows.append(row)
        return begin + r"\\".join(rows) + end

    content = pat.sub(_fix, content)
    return content, count


def postprocess_tex(tex_path: Path) -> None:
    """Fix common MinerU/pandoc artifacts in the generated .tex."""
    print("[4/4] postprocess")
    content = tex_path.read_text("utf-8")
    tags: list[str] = []

    # double-wrapped equations: \begin{equation}\[...\]\end{equation}
    pat = re.compile(
        r"\\begin\{equation\}\s*\\\[\s*(.*?)\s*\\\]\s*\\end\{equation\}",
        re.DOTALL,
    )
    if pat.search(content):
        content = pat.sub(r"\\begin{equation}\n\1\n\\end{equation}", content)
        tags.append("double_eq")

    # empty math environments
    before = len(content)
    content = re.sub(
        r"\\begin\{(equation|align|gather)\*?\}\s*\\end\{\1\*?\}",
        "",
        content,
    )
    if len(content) != before:
        tags.append("empty_env")

    # collapse runs of blank lines
    content = re.sub(r"\n{4,}", "\n\n\n", content)

    # \begin{cases} outside math mode
    pat_c = re.compile(r"(?<![\$\\])\\begin\{cases\}(.*?)\\end\{cases\}", re.DOTALL)
    if pat_c.search(content):
        content = pat_c.sub(r"\\[\n\\begin{cases}\1\\end{cases}\n\\]", content)
        tags.append("cases_wrap")

    # \textbf/\textit inside inline math → \mathbf/\mathit
    def _mathify(m: re.Match[str]) -> str:
        s = m.group(0)
        return s.replace(r"\textbf{", r"\mathbf{").replace(r"\textit{", r"\mathit{")

    content = re.sub(r"\$[^$]+\$", _mathify, content)

    # split merged greek (second pass, pandoc can reintroduce them)
    content, n = _GREEK_RE.subn(_split_greek, content)
    if n:
        tags.append(f"greek={n}")

    # unmatched \left / \right
    content, n = _fix_delimiters(content)
    if n:
        tags.append(f"delim={n}")

    # extra alignment tabs
    content, n = _fix_alignment_tabs(content)
    if n:
        tags.append(f"tabs={n}")

    # broken image paths pointing at tmp dirs
    pat_img = re.compile(r"\\includegraphics(\[.*?\])?\{(/tmp|/var|C:\\)[^}]*\}")
    nb = len(pat_img.findall(content))
    if nb:
        content = pat_img.sub(r"% [image removed — temp path]", content)
        tags.append(f"broken_img={nb}")

    # leftover \? junk
    n = content.count("\\?")
    if n:
        content = content.replace("\\?", "")
        tags.append(f"junk_qmark={n}")

    # duplicate geometry package
    gm = list(re.finditer(r"\\usepackage\[.*?\]\{geometry\}", content))
    if len(gm) > 1:
        for m in reversed(gm[1:]):
            content = (
                content[: m.start()] + "% (dup geometry removed)" + content[m.end() :]
            )
        tags.append("dup_geometry")

    tex_path.write_text(content, "utf-8")
    if tags:
        print(f"      {', '.join(tags)}")


# ===================================================================
#  Image copy
# ===================================================================


def copy_images(work_dir: Path, output_dir: Path, tex_path: Path) -> None:
    """Copy extracted images next to .tex and fix paths."""
    img_dirs = list(work_dir.rglob("images"))
    if not img_dirs:
        return
    src = img_dirs[0]
    files = [f for f in src.iterdir() if f.is_file()]
    if not files:
        return
    dst = output_dir / "images"
    dst.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, dst / f.name)
    content = tex_path.read_text("utf-8")
    content = re.sub(re.escape(str(src)) + r"[/\\]", "images/", content)
    tex_path.write_text(content, "utf-8")
    print(f"      {len(files)} images → {dst}")


# ===================================================================
#  Trial compilation
# ===================================================================


def try_compile(tex_path: Path) -> None:
    """Run pdflatex once and report errors. Never fatal."""
    if not shutil.which("pdflatex"):
        return
    proc = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", tex_path.name],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=tex_path.parent,
        timeout=120,
    )
    errors = [l for l in proc.stdout.splitlines() if l.startswith("!")]
    output = [l for l in proc.stdout.splitlines() if "Output written" in l]
    if output:
        print(f"      {output[0].strip()}")
    if errors:
        uniq = sorted(set(errors))
        print(f"      {len(errors)} error(s), {len(uniq)} unique:")
        for e in uniq[:10]:
            print(f"        {e}")
    elif proc.returncode == 0:
        print("      clean compile ✓")
    for ext in (".aux", ".log", ".out"):
        p = tex_path.with_suffix(ext)
        if p.exists():
            p.unlink()


# ===================================================================
#  Dependency check
# ===================================================================


def check_deps() -> None:
    errs: list[str] = []
    try:
        import magic_pdf  # noqa: F401
    except ImportError:
        errs.append("magic-pdf missing — run ./setup.sh")
    if not shutil.which("pandoc"):
        errs.append(
            "pandoc missing — install it (pacman -S pandoc / apt install pandoc)"
        )
    if errs:
        for e in errs:
            print(f"  ✗ {e}", file=sys.stderr)
        sys.exit(1)


# ===================================================================
#  CLI
# ===================================================================


def main() -> None:
    p = argparse.ArgumentParser(
        prog="pdf2tex",
        description="Convert PDF to LaTeX (MinerU + pandoc)",
    )
    p.add_argument("input", type=Path, help="input PDF")
    p.add_argument("output", type=Path, help="output .tex")
    p.add_argument("--keep-md", action="store_true", help="save intermediate .md")
    p.add_argument("--keep-work", action="store_true", help="keep MinerU work dir")
    p.add_argument("--work-dir", type=Path, default=None, help="explicit work dir")
    p.add_argument("--no-images", action="store_true", help="skip image extraction")
    p.add_argument("--no-postprocess", action="store_true", help="skip .tex fixups")
    p.add_argument(
        "--no-compile-check", action="store_true", help="skip trial pdflatex"
    )
    args = p.parse_args()

    pdf_path = args.input.resolve()
    tex_path = args.output.resolve()

    if not pdf_path.exists():
        print(f"  ✗ not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)
    if pdf_path.suffix.lower() != ".pdf":
        print(f"  ✗ expected .pdf, got {pdf_path.suffix}", file=sys.stderr)
        sys.exit(1)

    check_deps()

    if args.work_dir:
        work_dir = args.work_dir.resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        work_dir = Path(tempfile.mkdtemp(prefix="pdf2tex_"))
        cleanup = not args.keep_work

    tex_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n  in:   {pdf_path}\n  out:  {tex_path}\n  work: {work_dir}\n")

    try:
        md = run_mineru(pdf_path, work_dir)
        md = clean_markdown(md)
        md_to_tex(md, tex_path)

        if not args.no_postprocess:
            postprocess_tex(tex_path)
        if not args.no_images:
            copy_images(work_dir, tex_path.parent, tex_path)
        if args.keep_md:
            dst = tex_path.with_suffix(".md")
            shutil.copy2(md, dst)
            print(f"      md saved → {dst}")
        if not args.no_compile_check:
            try:
                try_compile(tex_path)
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        print(f"\n  done → {tex_path}")
        print(f"  compile: pdflatex {tex_path.name}\n")

    except KeyboardInterrupt:
        print("\n  interrupted", file=sys.stderr)
        sys.exit(130)
    finally:
        if cleanup and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
