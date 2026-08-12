"""Render a Markdown file to PDF using Edge headless.

Usage:  python tools/md2pdf.py docs/HLD.md [more.md ...]
Output: same folder, same name, .pdf

No LaTeX, no pandoc. Edge ships with Windows.
"""

import subprocess
import sys
from pathlib import Path

import markdown

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

CSS = """
@page { size: A4; margin: 18mm 15mm; }
body { font: 10.5pt/1.55 "Segoe UI", system-ui, sans-serif; color: #1a1a1a; max-width: none; }
h1 { font-size: 22pt; border-bottom: 2px solid #333; padding-bottom: 6px; margin-top: 0; }
h2 { font-size: 15pt; margin-top: 26px; border-bottom: 1px solid #ccc; padding-bottom: 4px;
     page-break-after: avoid; }
h3 { font-size: 12pt; margin-top: 18px; page-break-after: avoid; }
h4 { font-size: 11pt; margin-top: 14px; page-break-after: avoid; }
p, li { orphans: 3; widows: 3; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 9pt;
        page-break-inside: avoid; }
th, td { border: 1px solid #bbb; padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #f0f0f0; font-weight: 600; }
tr:nth-child(even) td { background: #fafafa; }
code { font-family: Consolas, monospace; font-size: 9pt; background: #f4f4f4;
       padding: 1px 4px; border-radius: 3px; }
pre { background: #f7f7f7; border: 1px solid #ddd; border-radius: 4px; padding: 10px;
      overflow-x: auto; page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: 8pt; line-height: 1.35; }
blockquote { border-left: 3px solid #888; margin: 12px 0; padding: 4px 14px;
             color: #444; background: #f9f9f9; }
hr { border: none; border-top: 1px solid #ddd; margin: 22px 0; }
a { color: #0b5cad; text-decoration: none; }
li { margin: 3px 0; }
"""


def render(md_path: Path) -> Path:
    html_body = markdown.markdown(
        md_path.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
    )
    html_path = md_path.with_suffix(".html")
    html_path.write_text(
        f"<!doctype html><meta charset='utf-8'><title>{md_path.stem}</title>"
        f"<style>{CSS}</style>{html_body}",
        encoding="utf-8",
    )

    pdf_path = md_path.with_suffix(".pdf")
    subprocess.run(
        [
            str(EDGE),
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            html_path.as_uri(),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    html_path.unlink()  # scratch file, not a deliverable
    return pdf_path


if __name__ == "__main__":
    if not EDGE.exists():
        sys.exit(f"Edge not found at {EDGE}")
    args = sys.argv[1:] or ["docs/HLD.md"]
    for arg in args:
        src = Path(arg).resolve()
        if not src.exists():
            sys.exit(f"missing: {src}")
        out = render(src)
        assert out.exists() and out.stat().st_size > 1000, f"PDF looks empty: {out}"
        print(f"{src.name} -> {out}  ({out.stat().st_size // 1024} KB)")
