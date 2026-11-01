"""导出提交材料：docs/*.md → Word（mermaid 图转 PNG 插进去），再打一个源码压缩包。

赛方要求文档以 Word 提交（线上答疑会），Markdown 原文随源码包一起交。输出到 dist/submission/（不进仓库）：
  文档/NN-xxx.docx            每份文档一个 Word
  言行SayDo-参赛文档合集.docx  PRD、技术文档、架构图、安全自评、测试报告、部署说明合成一份，每份另起一页
  言行SayDo-技术文档.pdf       提交页「技术文档」：架构图 + 产品技术文档（核心算法、安全设计）
  言行SayDo-安全自评报告.pdf   提交页「安全自评报告」
  言行SayDo-参赛文档合集.pdf   合集的 PDF 版
  三份 PDF 第一页是封面（文档名、队伍名称和成员），背景插画是 scripts/assets/cover-art.png，用 scripts/merge_pdf.py（pypdf）拼在正文前
  saydo-agent-源码.zip         git archive 当前提交：只含进了仓库的文件，.env、data/*.db、node_modules 都不在里面

用法：make docx（或 python3 scripts/export_docx.py）。需要 pandoc（brew install pandoc）、
apps/web 装好依赖和 Playwright Chromium（npx playwright install chromium），渲染 mermaid 要联网（从 jsDelivr 加载）。
本脚本只用标准库；拼封面的 merge_pdf.py 由 uv 临时装 pypdf 运行。
"""

import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
OUT = ROOT / "dist" / "submission"
COLLECTION = ["01-产品需求文档-PRD", "02-产品技术文档", "07-架构图", "05-安全自评报告", "06-测试报告", "04-部署说明"]
# 提交页能直接预览 PDF：技术文档（架构图在前）、安全自评、合集各出一份
PDFS = {"言行SayDo-技术文档": ["07-架构图", "02-产品技术文档"], "言行SayDo-安全自评报告": ["05-安全自评报告"],
        "言行SayDo-参赛文档合集": COLLECTION}
PDF_BREAK = '\n\n<div style="break-after: page"></div>\n\n'
# 每份 PDF 前面加一页封面：文档名和一句说明；背景插画（scripts/assets/cover-art.png）左上留白，文字排在留白处
COVERS = {"言行SayDo-技术文档": ("技术文档", "本文档收录系统架构图和产品技术文档，写明核心算法与安全设计。"),
          "言行SayDo-安全自评报告": ("安全自评报告", "本报告按权限分级、熔断止损、审计追溯和红队测试逐项给出自评结果。"),
          "言行SayDo-参赛文档合集": ("参赛文档合集", "本合集收录产品需求、技术文档、架构图、安全自评、测试报告和部署说明。")}
TEAM_NAME = "坷垃的鸡屁踢"
TEAM = [("队长", "李依伦", "东北大学 · 会计学"), ("队员", "卫高钰", "同济大学 · 管理科学与工程")]
COVER_ART = ROOT / "scripts/assets/cover-art.png"
COVER_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
@page { size: A4; margin: 0; }
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { width: 210mm; height: 297mm; }
body { position: relative; overflow: hidden; background: #fffaf9; color: #1f2329;
       font-family: "PingFang SC", "Microsoft YaHei", sans-serif; }
.art { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.text { position: absolute; left: 24mm; top: 30mm; right: 24mm; }
.event { font-size: 11pt; letter-spacing: 0.12em; color: #c8102e; }
.name { margin-top: 14mm; font-size: 60pt; font-weight: 700; line-height: 1; letter-spacing: 0.06em; }
.en { margin-top: 4mm; font-size: 26pt; font-weight: 600; letter-spacing: 0.01em; }
.lead { margin-top: 6mm; font-size: 12.5pt; color: #4e5562; }
.rule { margin-top: 12mm; width: 36mm; border-top: 1px solid #c8102e; }
.doc { margin-top: 8mm; font-size: 30pt; font-weight: 700; color: #a30d25; letter-spacing: 0.08em; }
.desc { margin-top: 4mm; max-width: 150mm; font-size: 11pt; line-height: 1.7; color: #4e5562; }
.team { margin-top: 16mm; display: grid; grid-template-columns: auto auto 1fr; column-gap: 7mm; row-gap: 4mm;
        font-size: 12pt; align-items: baseline; }
.team .role { color: #c8102e; font-size: 10.5pt; letter-spacing: 0.1em; }
.team .who { font-weight: 600; letter-spacing: 0.08em; }
.team .org { color: #4e5562; }
.team .squad { grid-column: 2 / 4; font-weight: 600; letter-spacing: 0.08em; color: #a30d25; }
.foot { position: absolute; left: 24mm; bottom: 22mm; font-size: 10.5pt; color: #4e5562; line-height: 1.8; }
.foot b { color: #c8102e; font-weight: 600; letter-spacing: 0.12em; }
</style></head><body>
<img class="art" src="{art}" alt="">
<div class="text">
  <div class="event">「银行 AI 智能体」赛题参赛作品</div>
  <div class="name">言行</div>
  <div class="en">SayDo Agent</div>
  <div class="lead">面向家庭财务管理者的手机银行 AI 助手</div>
  <div class="rule"></div>
  <div class="doc">{doc}</div>
  <div class="desc">{desc}</div>
  <div class="team">{team}</div>
</div>
<div class="foot"><b>言出即办，行必有据</b><br>2026 年 11 月</div>
</body></html>"""
# 明亮版式，标题用产品主题色；图按页宽缩放，太高的图限高，表格细边框
PDF_CSS = """
body { font-family: "PingFang SC", "Microsoft YaHei", sans-serif; font-size: 10.5pt; line-height: 1.65; color: #1f2329; max-width: none; margin: 0; padding: 0; }
h1, h2, h3, h4 { color: #a30d25; line-height: 1.35; break-after: avoid; }
h1 { font-size: 20pt; border-bottom: 2px solid #c8102e; padding-bottom: 4px; }
h2 { font-size: 15pt; border-left: 4px solid #c8102e; padding-left: 8px; }
h3 { font-size: 12.5pt; }
a { color: #c8102e; text-decoration: none; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 9pt; break-inside: auto; }
th, td { border: 1px solid #d9dce1; padding: 4px 6px; vertical-align: top; }
th { background: #fcebee; }
tr { break-inside: avoid; }
img { max-width: 100%; max-height: 240mm; display: block; margin: 8px auto; }
pre, code { font-family: "SF Mono", Menlo, monospace; font-size: 8.5pt; }
pre { background: #f6f7f9; padding: 8px; white-space: pre-wrap; word-break: break-all; border-radius: 4px; }
blockquote { margin: 8px 0; padding: 6px 12px; background: #fcebee; border-left: 3px solid #c8102e; color: #3a3f47; }
nav#TOC { break-after: page; }
nav#TOC ul { list-style: none; padding-left: 1.2em; }
header#title-block-header { display: none; }
"""

MERMAID = re.compile(r"^```mermaid\n(.*?)^```\n", re.S | re.M)
# 指向仓库里其他文件的相对链接（Word 里点不开）只留文字；http 链接照常保留
LOCAL_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\((?!https?://|mailto:|#)[^)]*\)")
MAX_IMAGE_HEIGHT = 21 * 360000  # 图最高 21 cm（A4 去掉上下边距约 24.6 cm，留出标题和图下文字），单位 EMU
PAGE_BREAK = '\n```{=openxml}\n<w:p><w:r><w:br w:type="page"/></w:r></w:p>\n```\n'


def fail(text: str) -> None:
    print(f"✗ {text}")
    sys.exit(1)


def prepare(md: str, images: Path, jobs: list[dict]) -> str:
    """mermaid 代码块换成图片引用（同一张图只渲染一次），本地链接去掉链接只留文字。"""

    def image(m: re.Match) -> str:
        code = m.group(1)
        png = images / f"{hashlib.sha1(code.encode()).hexdigest()[:12]}.png"
        if not any(j["out"] == str(png) for j in jobs):
            jobs.append({"code": code, "out": str(png)})
        return f"![]({png})\n"

    def local_image(m: re.Match) -> str:
        """文档里引用的本地截图：按 docs/ 解析相对路径；webp 转成 PNG（旧版 Word 不认 webp），用 macOS 自带的 sips。"""
        alt, src = m.group(1), m.group(2)
        path = (DOCS / src).resolve()
        if path.suffix.lower() == ".webp":
            png = images / f"{path.stem}.png"
            subprocess.run(["sips", "-s", "format", "png", str(path), "--out", str(png)], capture_output=True, check=True)
            path = png
        return f"![{alt}]({path})"

    md = re.sub(r"!\[([^\]]*)\]\((?!https?://|/)([^)\s]+)\)", local_image, MERMAID.sub(image, md))
    return LOCAL_LINK.sub(r"\1", md)


def set_dpi(png: Path, dpi: int) -> None:
    """写 PNG 的 pHYs 块：mermaid 图按 2 倍像素截的，标成 192 DPI，pandoc 才会按原尺寸放进 Word。"""
    data = png.read_bytes()
    chunks, pos = [], 8
    while pos < len(data):
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk = data[pos:pos + 12 + size]
        if chunk[4:8] != b"pHYs":
            chunks.append(chunk)
        pos += 12 + size
    ppm = round(dpi / 0.0254)
    body = b"pHYs" + struct.pack(">IIB", ppm, ppm, 1)
    phys = struct.pack(">I", 9) + body + struct.pack(">I", zlib.crc32(body))
    png.write_bytes(data[:8] + chunks[0] + phys + b"".join(chunks[1:]))


def fit_height(docx: Path) -> None:
    """pandoc 只按页宽缩图，很高的流程图会超出一页：按最大高度等比缩小。"""
    with zipfile.ZipFile(docx) as z:
        files = {n: z.read(n) for n in z.namelist()}

    def shrink(m: re.Match) -> str:
        block = m.group(0)
        cy = int(re.search(r'cy="(\d+)"', block).group(1))
        if cy <= MAX_IMAGE_HEIGHT:
            return block
        k = MAX_IMAGE_HEIGHT / cy
        return re.sub(r'(c[xy])="(\d+)"', lambda a: f'{a.group(1)}="{round(int(a.group(2)) * k)}"', block)

    files["word/document.xml"] = re.sub(r"<w:drawing>.*?</w:drawing>", shrink,
                                        files["word/document.xml"].decode(), flags=re.S).encode()
    with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in files.items():
            z.writestr(n, data)


def reference_doc(path: Path) -> None:
    """pandoc 默认样式上改三处：A4 纸、2.54 cm 页边距；中文字体用微软雅黑（Windows 和装了 Office 的 Mac 都有）；表格加细边框。"""
    subprocess.run(["pandoc", "-o", str(path), "--print-default-data-file", "reference.docx"], check=True)
    with zipfile.ZipFile(path) as z:
        files = {n: z.read(n) for n in z.namelist()}
    page = ('<w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
            'w:left="1440" w:header="851" w:footer="992" w:gutter="0"/>')
    files["word/document.xml"] = files["word/document.xml"].decode().replace(
        "</w:footnotePr>", "</w:footnotePr>" + page, 1).encode()
    styles = files["word/styles.xml"].decode()
    styles = re.sub(r'w:eastAsiaTheme="minorEastAsia"', 'w:eastAsia="Microsoft YaHei"', styles)
    border = "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>'
                     for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
    styles = re.sub(r'(<w:style w:type="table"[^>]*w:styleId="Table">.*?<w:tblPr>)',
                    rf"\1<w:tblBorders>{border}</w:tblBorders>", styles, count=1, flags=re.S)
    files["word/styles.xml"] = styles.encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in files.items():
            z.writestr(n, data)


def to_docx(md: str, out: Path, ref: Path) -> None:
    subprocess.run(["pandoc", "-f", "gfm+raw_attribute", "-t", "docx", "--reference-doc", str(ref),
                    "--toc", "--toc-depth=2", "-M", "toc-title=目录", "-o", str(out)],
                   input=md.encode(), check=True)
    fit_height(out)


def to_pdf(md: str, out: Path, title: str, tmp: Path) -> None:
    """pandoc 出带静态目录的 HTML（图片内嵌），再用 Chromium 打印成 A4 PDF。"""
    css, html = tmp / "pdf.css", tmp / f"{out.stem}.html"
    css.write_text(PDF_CSS)
    subprocess.run(["pandoc", "-f", "gfm+raw_html", "-t", "html5", "--standalone", "--embed-resources", "--css", str(css),
                    "--toc", "--toc-depth=2", "-M", f"title={title}", "-M", "toc-title=目录", "-o", str(html)],
                   input=md.encode(), check=True)
    body = tmp / f"{out.stem}-body.pdf"
    subprocess.run(["node", str(ROOT / "scripts/render_pdf.mjs"), str(html), str(body), title], check=True)
    doc, desc = COVERS[out.stem]
    team = f'<span class="role">队伍</span><span class="squad">{TEAM_NAME}</span>' + "".join(f'<span class="role">{r}</span><span class="who">{n}</span><span class="org">{o}</span>' for r, n, o in TEAM)
    cover_html, cover = tmp / f"{out.stem}-cover.html", tmp / f"{out.stem}-cover.pdf"
    cover_html.write_text(COVER_HTML.replace("{art}", COVER_ART.as_uri()).replace("{doc}", doc)
                          .replace("{desc}", desc).replace("{team}", team))
    subprocess.run(["node", str(ROOT / "scripts/render_pdf.mjs"), str(cover_html), str(cover)], check=True)
    subprocess.run(["uv", "run", "--no-project", "--quiet", "--with", "pypdf", "python", str(ROOT / "scripts/merge_pdf.py"),
                    str(out), str(cover), str(body)], check=True)


def main() -> None:
    if shutil.which("pandoc") is None:
        fail("没找到 pandoc：brew install pandoc")
    if not (ROOT / "apps/web/node_modules/playwright").exists():
        fail("apps/web 没装依赖：cd apps/web && npm install && npx playwright install chromium")
    if shutil.which("uv") is None:
        fail("没找到 uv（拼封面要用它临时装 pypdf）：curl -LsSf https://astral.sh/uv/install.sh | sh")
    # 只清掉本脚本的产物：PPT、演示视频等另外放进 dist/submission/ 的文件保留
    shutil.rmtree(OUT / "文档", ignore_errors=True)
    for old in [*OUT.glob("言行*-参赛文档合集.*"), *OUT.glob("言行SayDo-技术文档.pdf"), *OUT.glob("言行SayDo-安全自评报告.pdf"),
                *OUT.glob("*源码.zip")]:
        old.unlink()
    (OUT / "文档").mkdir(parents=True)
    with tempfile.TemporaryDirectory(prefix="saydo-docx-") as tmp:
        tmp = Path(tmp)
        images = tmp / "images"
        images.mkdir()
        jobs: list[dict] = []
        docs = {p.stem: prepare(p.read_text(), images, jobs) for p in sorted(DOCS.glob("[0-9][0-9]-*.md"))}

        (tmp / "jobs.json").write_text(json.dumps(jobs, ensure_ascii=False))
        if subprocess.run(["node", str(ROOT / "scripts/render_mermaid.mjs"), str(tmp / "jobs.json")]).returncode != 0:
            fail("有 mermaid 图没渲染出来，看上面的报错")
        for job in jobs:
            set_dpi(Path(job["out"]), 192)

        ref = tmp / "reference.docx"
        reference_doc(ref)
        for stem, md in docs.items():
            to_docx(md, OUT / "文档" / f"{stem}.docx", ref)
            print(f"  文档/{stem}.docx")
        to_docx(PAGE_BREAK.join(docs[s] for s in COLLECTION), OUT / "言行SayDo-参赛文档合集.docx", ref)
        print("  言行SayDo-参赛文档合集.docx")
        for name, stems in PDFS.items():
            to_pdf(PDF_BREAK.join(docs[s] for s in stems), OUT / f"{name}.pdf", name.replace("言行SayDo-", "言行 SayDo Agent · "), tmp)
            print(f"  {name}.pdf")

    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    subprocess.run(["git", "archive", "--format=zip", "--prefix=saydo-agent/", "-o", str(OUT / "saydo-agent-源码.zip"), "HEAD"],
                   cwd=ROOT, check=True)
    print(f"  saydo-agent-源码.zip（提交 {head}{'；工作区还有没提交的改动，不在包里' if dirty else ''}）")
    print(f"\n✓ 输出在 {OUT.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
