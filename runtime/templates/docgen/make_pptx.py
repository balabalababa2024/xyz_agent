"""Build a 16:9 .pptx from a small JSON spec.

Usage: python make_pptx.py spec.json out.pptx

Spec: {"title","subtitle","author","theme":"#0F766E","slides":[{"title","subtitle","bullets":[],"paragraphs":[]}]}
"""

import json
import sys

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt


def rgb(value, default="0F766E"):
    value = (value or default).lstrip("#")
    try:
        return RGBColor.from_string(value.upper())
    except Exception:  # noqa: BLE001 - bad hex in spec
        return RGBColor.from_string(default)


def main():
    spec_path, out_path = sys.argv[1], sys.argv[2]
    with open(spec_path, encoding="utf-8") as fh:
        spec = json.load(fh)

    theme = rgb(spec.get("theme"))
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    cover = prs.slides.add_slide(blank)
    bar = cover.shapes.add_shape(1, Inches(0), Inches(2.3), Inches(13.333), Inches(0.12))
    bar.fill.solid()
    bar.fill.fore_color.rgb = theme
    bar.line.fill.background()
    box = cover.shapes.add_textbox(Inches(0.9), Inches(2.6), Inches(11.5), Inches(1.2))
    p = box.text_frame.paragraphs[0]
    p.text = spec.get("title", "未命名演示")
    p.font.size = Pt(38)
    p.font.bold = True
    p.font.name = "Microsoft YaHei"
    if spec.get("subtitle"):
        sub = cover.shapes.add_textbox(Inches(0.95), Inches(3.7), Inches(11.3), Inches(0.7))
        sp = sub.text_frame.paragraphs[0]
        sp.text = spec["subtitle"]
        sp.font.size = Pt(18)
        sp.font.name = "Microsoft YaHei"
        sp.font.color.rgb = theme
    if spec.get("author"):
        meta = cover.shapes.add_textbox(Inches(0.95), Inches(6.4), Inches(11.3), Inches(0.5))
        mp = meta.text_frame.paragraphs[0]
        mp.text = spec["author"]
        mp.font.size = Pt(12)
        mp.font.name = "Microsoft YaHei"

    for slide_spec in spec.get("slides", []):
        slide = prs.slides.add_slide(blank)
        accent = slide.shapes.add_shape(1, Inches(0.7), Inches(1.18), Inches(1.6), Inches(0.09))
        accent.fill.solid()
        accent.fill.fore_color.rgb = theme
        accent.line.fill.background()

        title_box = slide.shapes.add_textbox(Inches(0.68), Inches(0.55), Inches(11.6), Inches(0.8))
        tp = title_box.text_frame.paragraphs[0]
        tp.text = slide_spec.get("title", "")
        tp.font.size = Pt(28)
        tp.font.bold = True
        tp.font.name = "Microsoft YaHei"

        body = slide.shapes.add_textbox(Inches(0.85), Inches(1.55), Inches(11.3), Inches(5.2))
        tf = body.text_frame
        tf.word_wrap = True
        first = True
        if slide_spec.get("subtitle"):
            p0 = tf.paragraphs[0]
            p0.text = slide_spec["subtitle"]
            p0.font.size = Pt(15)
            p0.font.name = "Microsoft YaHei"
            p0.font.color.rgb = theme
            first = False
        for line in slide_spec.get("paragraphs", []):
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            p.text = line
            p.font.size = Pt(16)
            p.font.name = "Microsoft YaHei"
        for bullet in slide_spec.get("bullets", []):
            p = tf.paragraphs[0] if first else tf.add_paragraph()
            first = False
            p.text = f"• {bullet}"
            p.font.size = Pt(17)
            p.font.name = "Microsoft YaHei"
            p.space_after = Pt(8)

    prs.save(out_path)
    print(f"PPTX_OK {out_path} slides={len(prs.slides)}")


if __name__ == "__main__":
    main()
