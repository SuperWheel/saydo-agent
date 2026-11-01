"""把封面 PDF 和正文 PDF 按顺序拼成一份。pypdf 原样搬运页面内容，图片不重新编码，体积约等于两份之和。

用法：uv run --no-project --with pypdf python scripts/merge_pdf.py <out.pdf> <a.pdf> <b.pdf> ...
"""

import sys

from pypdf import PdfWriter

out, *parts = sys.argv[1:]
writer = PdfWriter()
for part in parts:
    writer.append(part)
with open(out, "wb") as f:
    writer.write(f)
