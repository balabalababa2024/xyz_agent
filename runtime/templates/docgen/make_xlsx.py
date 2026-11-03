"""Build a .xlsx from a small JSON spec.

Usage: python make_xlsx.py spec.json out.xlsx

Spec: {"sheet","title","columns":[],"rows":[[]],"widths":[],"freeze":true}
"""

import json
import sys

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill("solid", fgColor="0F766E")
HEADER_FONT = Font(color="FFFFFF", bold=True, name="Microsoft YaHei")
TITLE_FONT = Font(bold=True, size=14, name="Microsoft YaHei")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def main():
    spec_path, out_path = sys.argv[1], sys.argv[2]
    with open(spec_path, encoding="utf-8") as fh:
        spec = json.load(fh)

    wb = Workbook()
    ws = wb.active
    ws.title = spec.get("sheet") or "Sheet1"

    columns = spec.get("columns") or []
    rows = spec.get("rows") or []
    title = spec.get("title")

    start_row = 1
    if title:
        ws.cell(row=1, column=1, value=title).font = TITLE_FONT
        if columns:
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(columns))
            ws.cell(row=1, column=1).alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 26
        start_row = 2

    header_row = start_row
    for i, name in enumerate(columns, start=1):
        cell = ws.cell(row=header_row, column=i, value=str(name))
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER
    ws.row_dimensions[header_row].height = 22

    for r, row in enumerate(rows, start=header_row + 1):
        for c, value in enumerate(row[: len(columns)], start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.border = BORDER
            cell.alignment = Alignment(vertical="center")

    widths = spec.get("widths") or []
    for i in range(max(len(columns), len(widths))):
        letter = get_column_letter(i + 1)
        ws.column_dimensions[letter].width = float(widths[i]) if i < len(widths) else 16

    if spec.get("freeze", True) and columns:
        ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

    wb.save(out_path)
    print(f"XLSX_OK {out_path}")


if __name__ == "__main__":
    main()
