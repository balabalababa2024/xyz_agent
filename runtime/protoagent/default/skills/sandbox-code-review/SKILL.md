---
name: sandbox-code-review
description: 在隔离沙箱内审查代码、跑静态检查或测试并定位报错。当用户要求"审查这段代码/这个仓库""跑一下测试看哪里挂了""复现这个报错"时使用。所有文件与命令操作必须走 sbx_* 工具，不得使用宿主机的文件或命令工具。
---

# sandbox-code-review

在隔离沙箱里检查代码，产出结论。**不要修改宿主仓库**：所有写入都发生在沙箱内。

## 前置

- 只要涉及代码执行，一律用 `sbx_*` 工具；原生 `read_file` / `run_command` / `execute_code` 已被禁用，调用会失败。
- 若任一 `sbx_*` 工具返回以 `ERROR:` 开头的内容（例如沙箱服务不可达），**最多重试一次**，然后把原文告诉用户并说明"沙箱未就绪，无法执行"，不要反复重试或改用宿主机命令。
- 与内置 `/code-review`（PR 评审工作流）区分：本技能面向本地代码/片段，不依赖 GitHub。

## 步骤

1. `sbx_sandbox_info`：确认是否已有沙箱。返回 `no live sandbox` 时用 `sbx_sandbox_create` 起一个（默认 `python:3.11-slim`，工作目录 `/workspace`）。

2. 把代码放进沙箱，按来源二选一：
   - 用户给了宿主路径：先 `sbx_run_command("mkdir -p /workspace/repo")`，再 `sbx_upload_file(local_path="D:\\...\\main.py", sandbox_path="/workspace/repo/main.py")`（目录请逐文件上传，或让用户先打包成单文件）。
   - 用户直接粘贴代码：`sbx_write_file(path="/workspace/repo/main.py", data=<代码>)`。

3. 摸清结构：`sbx_list_dir(path="/workspace/repo", depth=2)`，必要时 `sbx_find_files(pattern="*.py", path="/workspace/repo")`。

4. 按语言跑检查（`timeout_s` 给足，长任务用 300）：
   - Python：`sbx_run_command(command="cd /workspace/repo && python -m compileall -q . && python -m pytest -q 2>&1 | tail -40", timeout_s=300)`
   - Node：`sbx_run_command(command="cd /workspace/repo && npm ci --no-audit --no-fund && npm test", timeout_s=300)`
   - 依赖装不上（沙箱无外网）时跳过安装，只跑语法级检查，并在结论里标明"未安装依赖"。

5. 需要找具体写法或调用点：`sbx_search_files(pattern="TODO|FIXME", path="/workspace/repo", regex=True)`。

6. 需要看上下文：`sbx_read_file(path="/workspace/repo/xxx.py", offset=0, limit=8000)`（`offset` / `limit` 以字节计，大文件分页读）。

7. 输出结论：
   - 先给 `exit_code` 与关键 stdout/stderr 摘要（不要把整段日志原样贴出）；
   - 再按「阻断 / 建议 / 提示」三档列问题，每条带文件与行号；
   - 明确说明本次只在沙箱内运行，宿主代码未被改动。

## 结束

- 沙箱默认复用，**不要**在每次审查后销毁；用户明确说"清理"或任务结束时才 `sbx_sandbox_destroy(action="destroy")`。
- 若产出了报告文件（如测试日志），用 `sbx_download_file(sandbox_path="/workspace/report.txt", local_path="<仓库根目录>/out/report.txt")` 取回后再告知路径。
