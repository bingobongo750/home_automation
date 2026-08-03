#!/usr/bin/env python3
"""Render MANUAL.md to MANUAL.pdf — a printable, bench-side copy of the build manual.

The pipeline is pandoc (GitHub-flavored markdown -> HTML) -> a print stylesheet
(tools/manual_print.css) -> Chromium's print-to-PDF via Playwright. It exists because
this machine has no PDF engine pandoc can drive: no LaTeX, no wkhtmltopdf, no weasyprint.

Run from the server/ directory with the project venv:

    .venv/bin/python tools/build_manual_pdf.py                 # -> <repo>/MANUAL.pdf
    .venv/bin/python tools/build_manual_pdf.py --keep-html     # also keep the HTML, for CSS work

Needs `pandoc` on PATH (or at the anaconda location below) and `playwright` plus its
chromium in the venv:

    .venv/bin/pip install playwright
    .venv/bin/python -m playwright install chromium-headless-shell

Deliberately NOT in requirements.txt: those are dev-machine-only tools, and the Pi
should never be asked to install a browser to run the hub.

Two layout traps worth knowing before editing the CSS, both of which produce a PDF that
looks fine until you check page edges:

  * Page margins are set ONLY in page.pdf() below. A `@page { margin }` rule in the CSS
    silently overrides them, shrinking the page box while the header/footer templates
    stay put — so body text prints on top of the footer.
  * A hanging indent on `pre code` indents every line except the first, because pandoc
    puts a whole code block inside one <code> element.
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SRC = REPO / "MANUAL.md"
CSS = HERE / "manual_print.css"

# Footer sits in the bottom page margin; keep its side padding equal to the pdf() margin
# so it lines up with the text column.
FOOTER = """<div style="width:100%;padding:0 20mm;font:400 7.5pt 'SF Mono',Menlo,monospace;
 letter-spacing:.08em;color:#8d968f;display:flex;justify-content:space-between;">
 <span>SMART HOME HUB &nbsp;·&nbsp; MANUAL</span>
 <span><span class="pageNumber"></span>&nbsp;/&nbsp;<span class="totalPages"></span></span>
</div>"""


def find_pandoc() -> str:
    pandoc = shutil.which("pandoc") or "/opt/anaconda3/bin/pandoc"
    if not Path(pandoc).exists():
        sys.exit("pandoc not found — install it, or fix the path in this script.")
    return pandoc


def git_rev() -> str:
    """Short SHA for the masthead, so a printed copy says which revision it describes."""
    try:
        return subprocess.check_output(
            ["git", "-C", str(REPO), "log", "-1", "--format=%h"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def build_html() -> str:
    """pandoc's body HTML, restructured into masthead / callout / contents / body.

    The line-index assertions are intentional: they fail loudly if MANUAL.md's opening
    changes shape, rather than quietly emitting a mangled first page.
    """
    body = subprocess.check_output(
        [find_pandoc(), "-f", "gfm", "-t", "html5", "--highlight-style=tango", str(SRC)],
        text=True,
    )
    lines = body.split("\n")

    # 0: the H1. Split "MANUAL — build & bring-up" into title and copper subtitle.
    assert lines[0].startswith('<h1 id="manual'), f"expected MANUAL.md to open with its H1, got: {lines[0]}"
    title, _, subtitle = lines[0].split(">", 1)[1].rsplit("</h1>", 1)[0].partition(" — ")
    rev = git_rev()
    meta = ["Smart Home Hub", "Raspberry Pi 4 &middot; Arduino Due"]
    if rev:
        meta.append(f"rev {rev}")
    meta.append(date.today().strftime("%-d %B %Y"))
    masthead = (
        '<header class="masthead"><div class="rule-top"></div>'
        f'<h1>{title}<span class="sub">{subtitle}</span></h1>'
        '<div class="meta">' + "".join(f"<span>{m}</span>" for m in meta) + "</div></header>"
    )

    # 1-2: the two intro paragraphs, set larger.
    lede = "".join(l.replace("<p>", '<p class="lede">', 1) for l in lines[1:3])
    assert lines[3] == "<hr />", f"expected a rule after the intro, got: {lines[3]}"

    # 4-6: the lighting warning, promoted to a bordered panel.
    assert 'id="-read-this-before-you-touch-the-lighting"' in lines[4], \
        f"expected the lighting warning as the first H2, got: {lines[4]}"
    assert lines[7] == "<hr />", f"expected a rule after the warning, got: {lines[7]}"
    callout = '<section class="callout">' + "".join(lines[4:7]) + "</section>"

    # 8-...: the contents list, boxed. Anchors are GitHub-style because of `-f gfm`,
    # which is what makes the manual's hand-written #anchors resolve.
    assert lines[8].startswith('<h2 id="contents"'), f"expected the Contents heading, got: {lines[8]}"
    end_toc = lines.index("</ul>", 9)
    toc = '<nav class="toc">' + "".join(lines[8:end_toc + 1]) + "</nav>"

    rest = "\n".join(lines[end_toc + 1:])
    # Leading section number out of each H2, so it can be set in copper.
    rest = re.sub(
        r'(<h2 id="[^"]*">)(\d+)\.\s',
        lambda m: f'{m.group(1)}<span class="num">{m.group(2)}.</span>',
        rest,
    )

    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
        "<title>MANUAL — build &amp; bring-up</title>\n"
        f"<style>{CSS.read_text()}</style>\n</head><body>\n"
        f"{masthead}\n{lede}\n{callout}\n{toc}\n{rest}\n</body></html>\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=REPO / "MANUAL.pdf", help="output PDF path")
    ap.add_argument("--keep-html", action="store_true",
                    help="also write the intermediate HTML next to the PDF")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is not installed in this venv — see the docstring.")

    html = build_html()
    with tempfile.TemporaryDirectory() as tmp:
        page_file = Path(tmp) / "manual.html"
        page_file.write_text(html)
        if args.keep_html:
            html_out = args.out.with_suffix(".html")
            html_out.write_text(html)
            print(f"wrote {html_out}")

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(page_file.as_uri(), wait_until="load")
            page.emulate_media(media="print")
            page.pdf(
                path=str(args.out),
                format="A4",
                print_background=True,
                display_header_footer=True,
                header_template="<div></div>",
                footer_template=FOOTER,
                # The only place margins may be set — see the docstring.
                margin={"top": "16mm", "bottom": "17mm", "left": "20mm", "right": "20mm"},
            )
            browser.close()

    print(f"wrote {args.out} ({args.out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
