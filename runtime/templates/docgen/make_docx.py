"""Build a .docx from a small JSON spec — no model-written layout code needed.

Usage: python make_docx.py spec.json out.docx

Spec: {"title","subtitle","author","sections":[{"heading","paragraphs":[],"bullets":[],"table":{"columns":[],"rows":[[]]}}]}
"""

import json
import sys

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt


def add_table(doc, table):
    cols = table.get("columns") or []
    rows = table.get("rows") or []
    if not cols:
        return
    t = doc.add_table(rows=1, cols=len(cols))
    t.style = "Light Grid Accent 1"
    for i, name in enumerate(cols):
        cell = t.rows[0].cells[i]
        cell.text = str(name)
        for run in cell.paragraphs[0].runs:
            run.bold = True
    for row in rows:
        cells = t.add_row().cells
        for i, value in enumerate(row[: len(cols)]):
            cells[i].text = "" if value is None else str(value)


def main():
    spec_path, out_path = sys.argv[1], sys.argv[2]
    with open(spec_path, encoding="utf-8") as fh:
        spec = json.load(fh)

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal.font.size = Pt(10.5)

    title = doc.add_heading(spec.get("title", "未命名文档"), level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if spec.get("subtitle"):
        sub = doc.add_paragraph(spec["subtitle"])
        sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in sub.runs:
            run.italic = True
    if spec.get("author"):
        meta = doc.add_paragraph(spec["author"])
        meta.alignment = WD_ALIGN_PARAGRAPH.CENTER

    for section in spec.get("sections", []):
        if section.get("heading"):
            doc.add_heading(section["heading"], level=1)
        for para in section.get("paragraphs", []):
            doc.add_paragraph(para)
        for bullet in section.get("bullets", []):
            doc.add_paragraph(bullet, style="List Bullet")
        if section.get("table"):
            add_table(doc, section["table"])

    doc.save(out_path)
    print(f"DOCX_OK {out_path}")


if __name__ == "__main__":
    main()
