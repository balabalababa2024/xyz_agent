---
name: doc-gen
description: 在隔离沙箱内生成 docx / xlsx / pptx / pdf / html 等文档，并把成品取回宿主目录。当用户要求"生成一份 Word/Excel/PPT/PDF""导出成网页""做个表格文件"时使用。文件写入与命令执行必须走 sbx_* 工具。
---

# doc-gen

在沙箱里生成文档，宿主只负责收成品。**固定三步：写 spec → 跑模板脚本 → 取回**，不要手写排版代码，也不要反复微调。

支持格式：`.docx`、`.xlsx`、`.pptx`、`.pdf`、`.html`。

## 前置

- 一律用 `sbx_*`；原生 `write_file` / `run_command` / `execute_code` 已禁用。
- 默认镜像 `docgen-base:2` 已预装全部依赖，并在 `/opt/docgen/` 内置三个模板脚本，**不要再 pip install**。
- 成品落宿主**仓库根目录下的 `out/`**；`sbx_download_file` 会自动建父目录并登记 Artifact 面板。仓库根目录以用户实际克隆位置为准（Windows 例如 `C:\repo\out\`，Linux 例如 `/opt/repo/out/`）。
- 工具返回 `ERROR:` 时最多重试一次；仍失败就直说该格式产不出来，可先给 HTML 兜底，禁止改扩展名冒充。

## 三步流程（docx / xlsx / pptx）

1. `sbx_sandbox_create` → 默认镜像直接可用，工作目录 `/workspace`。

2. 用 `sbx_write_file` 把 **spec.json** 写到 `/workspace/spec.json`。spec 只描述内容，版式由脚本负责：

   ```json
   {
     "title": "标题",
     "sections": [
       {"heading": "一、章节", "paragraphs": ["段落"], "bullets": ["要点"],
        "table": {"columns": ["列1", "列2"], "rows": [["a", "b"]]}}
     ]
   }
   ```

   - xlsx：`{"sheet","title","columns":[],"rows":[[]],"widths":[],"freeze":true}`
   - pptx：`{"title","subtitle","author","theme":"#0F766E","slides":[{"title","paragraphs":[],"bullets":[]}]}`

3. 一次运行 + 校验：

   ```text
   sbx_run_command("python /opt/docgen/make_docx.py /workspace/spec.json /workspace/out/report.docx")
   ```

   把 `make_docx.py` 换成 `make_xlsx.py` / `make_pptx.py`，输出扩展名随之改。成功标志是 `DOCX_OK` / `XLSX_OK` / `PPTX_OK slides=N`。

4. `sbx_download_file(sandbox_path="/workspace/out/<文件名>", local_path="<仓库根目录>/out/<文件名>")`（Windows 用反斜杠，如 `C:\\repo\\out\\report.docx`）。返回的字节数与 `artifact: id=…` 就是交付凭证；不知道仓库根目录时先问用户。

5. 汇报：宿主路径、真实格式、字节数；一次做完，不要追加“再美化一下”的多轮改写。

## PDF

模板不含 PDF。仍用脚本，但**只写一次**：

1. 上传宿主字体：`sbx_upload_file(local_path="<本机中文字体绝对路径>", sandbox_path="/workspace/fonts/simhei.ttf")`。常见位置：Windows `C:\\Windows\\Fonts\\simhei.ttf`，Linux `/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc`。
2. 写 `/workspace/gen_pdf.py`：`os.makedirs("/workspace/out", exist_ok=True)`；注册字体

   ```python
   from reportlab.pdfbase import pdfmetrics
   from reportlab.pdfbase.ttfonts import TTFont
   pdfmetrics.registerFont(TTFont("CJK", "/workspace/fonts/simhei.ttf"))
   ```

   字体缺失时改用 `UnicodeCIDFont("STSong-Light")`。
3. 跑一次 `python /workspace/gen_pdf.py`，`sbx_list_dir("/workspace/out")` 校验后下载。

## HTML

不需要模板：`sbx_write_file` 直接写 `/workspace/out/page.html`（UTF-8、内联 CSS、A4 打印样式），然后下载。适合作为沙箱不可用时的兜底。

## 常见坑

- 中文文件名两端都写全名；只传目录会失败。
- `python-docx` 不支持 `.doc`；用户要 `.doc` 只能给 `.docx` 并说明。
- `reportlab` 默认字体不含中文，必须先注册 CJK 字体。
- 表格数据量大时先写 CSV，再由脚本读 CSV 生成 xlsx，避免把长数据塞进工具调用。
- 不要在每份文档后销毁沙箱：容器常驻，按 `lifetime_min` 自然到期；用户明确要求清理时才 `sbx_sandbox_destroy(action="destroy")`。
