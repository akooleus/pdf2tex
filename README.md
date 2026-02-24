# pdf2tex

Converts PDF files to compilable `.tex` using [MinerU](https://github.com/opendatalab/MinerU) for OCR/layout/formula recognition and [pandoc](https://pandoc.org) for the final Markdown→LaTeX pass.

Works with math-heavy documents. Handles Cyrillic out of the box.

## What it actually does

1. **MinerU** parses the PDF — detects layout regions, runs OCR on text, recognizes formulas via UniMERNet, extracts images
2. **Cleanup pass** fixes soft hyphens from line breaks (`рассеи-\nвающим` → `рассеивающим`), replaces PDF Private Use Area unicode with ASCII, splits merged LaTeX commands (`\kappaM` → `\kappa M`), strips OCR junk
3. **pandoc** converts the cleaned Markdown to a standalone `.tex` with a Russian-ready preamble (`babel`, `amsmath`, `upgreek`, etc.)
4. **Post-processing** fixes unbalanced `\left`/`\right`, extra alignment tabs in tables, empty math environments, `\textbf` inside math mode, and other common artifacts
5. **Trial compilation** runs `pdflatex` once and reports any remaining errors

## Requirements

- Linux (tested on CachyOS/Arch, should work on Ubuntu/Fedora)
- NVIDIA GPU, 6GB+ VRAM (runs on CPU too, just slower)
- [uv](https://docs.astral.sh/uv/) for Python dependency management
- `pandoc` installed system-wide

## Install

```bash
git clone https://github.com/YOURUSER/pdf2tex.git ~/.local/share/pdf2tex
cd ~/.local/share/pdf2tex
./setup.sh
```

The setup script:
- runs `uv sync` (creates `.venv`, installs ~200 packages including PyTorch, MinerU, PaddlePaddle, detectron2)
- downloads model weights (~10 GB) from HuggingFace into `./models/`
- writes MinerU config to `~/magic-pdf.json`
- installs a wrapper script to `~/.local/bin/pdf2tex`

First run takes 10-20 minutes depending on your connection. Subsequent runs skip the download.

Make sure `~/.local/bin` is in your `$PATH` (it is by default on most distros).

## Usage

```bash
pdf2tex input.pdf output.tex
```

### Options

| Flag | Effect |
|---|---|
| `--keep-md` | Save intermediate Markdown next to the `.tex` |
| `--keep-work` | Don't delete MinerU's work directory |
| `--work-dir ./tmp` | Use a specific work directory |
| `--no-images` | Skip image extraction |
| `--no-postprocess` | Skip the `.tex` fixup pass |
| `--no-compile-check` | Skip trial `pdflatex` run |

### Compile the result

The output uses `pdflatex`-compatible packages (`fontenc`, `babel`, `amsmath`):

```bash
pdflatex output.tex
```

No special fonts required. Standard texlive is enough.

## Uninstall

```bash
~/.local/share/pdf2tex/setup.sh --uninstall
```

Removes `.venv`, downloaded models, the `~/.local/bin/pdf2tex` wrapper, and optionally `~/magic-pdf.json`. Leaves the project directory itself — delete it manually if you want (`rm -rf ~/.local/share/pdf2tex`).

## Limitations

These are real and worth knowing before you start:

- **Formula accuracy is ~80-90%.** Simple fractions and sums are fine. Multi-level nested fractions mostly work. Matrices and `cases` environments break sometimes — MinerU loses the 2D structure and flattens rows.
- **Scanned PDFs produce worse results** than born-digital ones. If the source PDF has selectable text, you're in better shape.
- **Speed is ~5-15 seconds per page** on an RTX 3060 6GB. A 400-page book takes 30-90 minutes.
- **The output will need manual review.** The cleanup pipeline catches most compilation errors automatically, but semantic mistakes in formulas (wrong variable, lost subscript) can only be caught by a human reading the output.
- **VRAM usage peaks at ~4-5 GB.** If you hit OOM errors, the script sets `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` which usually helps.

## CUDA version

The default config pulls PyTorch for CUDA 12.4. If your driver only supports 12.1 or 11.8, edit `pyproject.toml`:

```toml
[[tool.uv.index]]
name = "pytorch-cu124"
url = "https://download.pytorch.org/whl/cu121"  # change here
```

Then `rm -rf .venv && uv sync`.

## Project structure

```
~/.local/share/pdf2tex/
├── pyproject.toml   # dependencies (uv sync reads this)
├── setup.sh         # install / uninstall
├── convert.py       # the converter
├── .venv/           # created by uv sync
└── models/          # ~10 GB of neural net weights

~/.local/bin/pdf2tex # wrapper script (calls into the above)
```
