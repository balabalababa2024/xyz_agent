---
name: photo-qa
description: 拍照问答。用户发来一张图片（照片/截图/题目/票据/简历扫描件）并提出问题时，先用 vision_analyze 把图片转成文本，再按文本作答；需要跑代码或产出文档时，衔接 sandbox-code-review / doc-gen 技能。
---

# 拍照问答（photo-qa）

主对话模型是纯文本模型，不能直接看图。图片必须先经 `vision_analyze` 转成文本，再回答。

## 步骤

1. **拿到图片来源（优先级从高到低）**
   - **用户在聊天里直接贴图**：系统会给出一行 `[attached-image refs] … media id: image 1 = \`<id>\``，把那个 **id 原样**传给 `vision_analyze` 的 `image` 参数，不要自己拼路径。
   - 用户给的宿主路径（如 `D:\photos\question.jpg`）直接使用。
   - 仅当用户明确说图片在沙箱内时，才用 `sbx_download_file` 取回宿主，再把宿主路径交给 `vision_analyze`。
   - 公网图片可直接传 URL。

   > 看图本身不碰沙箱：不要为了找图去调用 `sbx_find_files` / `sbx_list_dir` / `sbx_sandbox_info` 等工具，也不要在回答里提沙箱、隧道或服务状态。

2. **调用 `vision_analyze`**
   - `image`：上一步得到的 media id / 路径 / URL。
   - `question`：把用户的**原始问题**写进去，而不是笼统的"描述这张图"。
     - 拍照问答题 → "请识别题目内容，并给出解题所需的关键信息"
     - 票据/表格 → "请按行转写表格内容，保留列名与数字"
     - 截图报错 → "请完整转写屏幕上的文字与错误信息"
   - 只问一遍；若返回 `ERROR:`，按提示处理（多为路径不存在或图片过大），不要盲目重试。

3. **基于文本作答**
   - 用 `vision_analyze` 返回的文本推理，**不要**声称自己直接"看到了图片"。
   - 识别不清时明确说明哪部分不确定，不要编造数字或文字。

## 与其他技能的衔接

| 场景 | 衔接 |
|---|---|
| 拍的是一段代码/报错截图，需要修 | 先 `vision_analyze` 转写 → 再走 `sandbox-code-review` |
| 拍的是一份表格/票据，需要出 Excel/Word | 先 `vision_analyze` 转写 → 再走 `doc-gen` |
| 拍的是简历，需要分析 | 先 `vision_analyze` 转写 → 用文本模型做结构化分析 |

## 边界

- 不猜测图片里看不清的内容；不确定就说明。
- 一张图只调一次 `vision_analyze`，同一个问题不要反复重试。
- 图片大于 12 MB 时先让用户压缩，或改用截图。
