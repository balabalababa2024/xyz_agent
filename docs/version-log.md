# 版本日志（Version Log）

- 项目：xyz_agent（基于 protoAgent 本地实例）
- 运行时底座：`protolabs-agent 0.219.0`（`C:\Users\wyr20\.local\bin\protoagent.exe`）
- 盒子根：`D:\Miaooaimzzz\xyz_agent\runtime\protoagent`，实例 `default`
- 版本口径：**当前开发版本为 `v0.0.1`**（包含阶段一~五、P0 优化与记忆分类/偏好守卫）；`v0.0.0` 保留为阶段一~二的历史基线。版本号只在发布节点上打 tag，未发布前改动累加到当前开发版本。
- 仓库整理（2026-11-03）：为让拉取者可直接运行，历史验证脚本与取证文件（`stage*-artifacts`、`verify-*.py`、探针/日志等）已移除；本文保留当时的结论与实测数据，相关路径仅作历史引用。

---

## 一、版本记录

| 版本 | 日期 | 内容 | 状态 |
|---|---|---|---|
| **v0.0.1** | 2026-11-03 | 阶段三~五完成；P0 优化（模板化生成 / 沙箱常驻 / 成品预览 / 流式）；记忆分类（方案 A）+ 确定性偏好守卫 `pref_guard`（方案 B） | 开发中（待建仓库，未发布） |
| v0.0.0 | 2026-10-28 起 | 历史基线：阶段一（底座与验证）、阶段二（沙箱工具层） | 已并入 v0.0.1 |

> `v0.0.0` 记录保留用于追溯；当前开发版本以 `v0.0.1` 为准。首个可发布节点确认后再定 `v0.1.0` 的归属。

### v0.0.0 → v0.0.1 — 累计变更明细（未发布）

> 按时间顺序记录。`v0.0.0` 覆盖阶段一~二；阶段三~五、P0 优化与记忆分类/偏好守卫自 2026-11-02 起计入 **v0.0.1**。

#### 里程碑 A = 阶段一：本地运行底座 + 可行性验证

**改动**

- 安装并确认 `protolabs-agent 0.219.0`；`--version` 非合法子命令，改用 `--help`（exit 0）+ 包元数据确认版本。
- 盒子根重定位到项目内 `runtime/protoagent`，绕开默认 C 盘路径下 `sqlite3.connect: unable to open database file` 的阻塞（独立进程同代码却成功，已排除磁盘/ACL/锁/损坏等假设）。
- 固定必需环境变量：`RUST_LOG=warn`、`PROTOAGENT_BOX_ROOT`、`PROTOAGENT_INSTANCE`、`UV_DEFAULT_INDEX`（清华镜像）。
- 落盘确认：`checkpoints.db`、`tasks/issues.db`、`telemetry.db`、`metrics.db`、`ledger.db`、`a2a-*.db`、`skills.db`（FTS5，22 条技能索引）。
- 技能加载路径确认：`<instance_root>/skills/<slug>/SKILL.md`，重启即索引。
- `tools.disabled` 机制实测：只按工具名精确摘除，**不连带摘除插件注册的工具**；禁用 10 个原生工具后 `/api/tools` count 74 → 64，插件工具 `sbx_ping` 仍 `enabled=true`。

**产物**：`docs/stage-1-verification.md`、`docs/stage1-artifacts/01–24`。

**遗留（待真实模型）**：对话持久化写入-重启-读回、`load_skill` 对话链路、模型驱动的插件工具调用。

#### 里程碑 B = 阶段二：沙箱工具层 `sbx_*`（当前进行中）

**改动**

- 新增插件 `runtime/protoagent/default/plugins/sbx_tools/`（manifest `version: 0.0.0`，`config_section: sbx_tools`，`secrets: [api_key]`）。
- 14 个工具：
  - 文件：`sbx_read_file`、`sbx_write_file`、`sbx_edit_file`、`sbx_delete_file`、`sbx_list_dir`、`sbx_find_files`、`sbx_search_files`
  - 执行：`sbx_run_command`、`sbx_execute_code`
  - 生命周期：`sbx_sandbox_create`、`sbx_sandbox_destroy`、`sbx_sandbox_info`
  - 进出：`sbx_upload_file`、`sbx_download_file`
- 单沙箱复用 + 空闲回收（默认 `idle_timeout_s=900`，容器寿命 60min）；失败即重置单例，返回带修复提示的 `ERROR:` 文本而非抛异常。
- `tools.disabled` 追加 12 项（含 `onboard_project`、`register_local_project`）；`/api/tools` 复核：`count=75`，`sbx_*` 14/14 `enabled=true`，原生 12 项 `enabled=false`。
- 移除阶段一取证插件 `sbx_probe`（`sbx_ping` 下线）。
- `opensandbox==1.2.0` 装入 protoagent 工具环境（副作用：`python-dateutil` 3.0.0 → 2.9.0.post0，重启无回归）。
- OpenSandbox Server 配置：`runtime/sandbox.toml`，端口 **8090**（8080 被本机 `java.exe` 占用，SDK 会拿到 HTML 404），store 落 `runtime/opensandbox/opensandbox.db`。

**产物**：`docs/stage2-tool-mapping.md`、`docs/stage-2-verification.md`、`docs/stage1-artifacts/30–33`、`41-sandbox-closedloop.json`。

**验收结果（2026-11-02，已完成）**：本机 WSL2 因系统组件损坏不可用，改为「阿里云 2C2G 跑 opensandbox-server + 本机 SSH 隧道 `localhost:18090 → 服务器 127.0.0.1:8080`」接入，全程不依赖本机虚拟化。

- SDK 级 `runtime/verify-sandbox.py`：**13/13 PASS**（create/读写/分页/list/find/replace/exec/内容检索/上传下载/宿主探针哈希未变/destroy）。
- 插件级 `runtime/sbx-plugin-live-test.py`：**14/14 PASS**（逐工具真实调用）。
- 服务重启后 `/api/tools`：14 个 `sbx_*` 全部 `enabled=true`，12 个原生工具 `enabled=false`；`/api/config` 已解析出 `sbx_tools.domain=localhost:18090`。

**实测修正的四个坑**（已落到代码）：① 跨网络必须 `use_server_proxy=True`，否则 SDK 直连沙箱映射端口超时；② `mode` 传 `755`/`644` 数字字面量（传 `0o755` 会被 `str()` 成 `"493"` 而报 ParseUint 错误）；③ `commands.run` 只能传字符串，argv 列表被当前服务端拒绝；④ `request_timeout` 默认 30s 不够，改 240s、`ready_timeout` 180s。

**运行前置**：隧道默认由插件自动维护（断线自动重开）；仅当云端 `opensandbox-server` 本身停止时才需登服务器手动启动。隧道不可用时 `sbx_*` 返回可读错误并熔断，不会退化到宿主执行。

**状态语义修正（2026-11-02）**：`sbx_sandbox_info` 在没有活跃容器时返回 `status=not_created (normal, lazily created on first use)` 并附带 `reachable=yes/no`，不再只说 “no live sandbox”（模型曾据此误报「沙箱未启动」）。同时 `SOUL.md` 明确：`not_created` 是常态，只有 `reachable=NO` 或工具返回 `ERROR:` 才算不可用。

**成品可见性修复（2026-11-03）**：Artifact 面板只显示**登记过**的产物，而 `sbx_download_file` 之前只落盘到 `out\`，没有调用 artifact 插件，所以界面上看不到。现改为下载成功后调用 `artifact.save_file` 服务（`plugin_id=sbx_tools`，`roots=[目标目录]`，线程内执行，失败仅告警不影响下载），工具回复附 `artifact: id=… v…`。已把既有 `out\望庐山瀑布-古风课件.pptx` 补登记进面板（`/api/plugins/artifact/history` 可见）。

**P0 体验与性能（2026-11-03）**

- **文档生成模板化**：新增 `runtime/templates/docgen/`（`make_docx.py`、`make_xlsx.py`、`make_pptx.py` + `examples/*.json`），版式由脚本负责、模型只写内容 spec；`doc-gen` 技能改为「写 spec → 跑脚本 → 取回」三步。
- **预构建镜像 `docgen-base:2`**：`FROM docgen-base:1`，把三个脚本与示例 COPY 到 `/opt/docgen/`；沙箱内不再现场装库。
- **沙箱常驻**：`idle_timeout_s: 0`（不按空闲回收）、`lifetime_min: 120`、默认镜像 `docgen-base:2`。插件注册阶段没有事件循环，**无法在启动时预热**（实测 `_schedule_prewarm` 被静默跳过），因此改为「首次调用建好（约 1.5s）后常驻复用」；该预热门控已移除，避免无效配置。
- **成品预览**：运行时环境安装 `python-pptx 1.0.2`、`openpyxl 3.1.5`、`reportlab 5.1.0`，Artifact 面板可对 pptx/xlsx/pdf 出文本预览。
- **流式输出**：控制台原生使用 SSE（`/api/events` 事件流 + `/api/chat/attend` 在场心跳），服务端无额外开关；本次确认控制台已持有事件流，无需改内核。
- **验收（插件级，不经过模型）**：`sbx_sandbox_create` → 三个模板脚本各跑一次 → `sbx_download_file` 回传，全部 `exit_code 0`、zip 校验通过：docx 37,149 B、xlsx 5,403 B、pptx 31,289 B（3 页），随后 `sbx_sandbox_destroy` 成功。测试容器、会话与临时文件已清理。
- **遗留**：模型级端到端复测当时被 DeepSeek 503/超时降级打断（降级模型未按指令执行），待主模型稳定后补测。

- **模型级端到端复测（2026-11-03 晚）**：主模型恢复后，一句自然语言请求即走完「建沙箱 → 写 spec → 跑 `make_docx.py` → 校验 → 下载」全流程：**38.5 秒**、产物 37,557 B（17 个 zip 分片，校验通过）、`sbx_download_file` 自动登记 Artifact（`a-1793703479622-8b207d` v1）。仍偏高的成本来自模型轮次：本轮 `prompt_tokens=188,924`（十余轮累积上下文），属于 P1 上下文瘦身的优化对象，不影响正确性。测试文件、会话、Artifact 条目与容器均已清理。

**记忆分类与偏好保护（2026-11-03，方案 A：提示层）**

底座本身没有「决策 / 错误 / 偏好」的消息分类器；它有的是类型化记忆（`memory_kind` + `delivery_policy`）和「always-on 记忆永不淘汰」的投影保证。本方案在不改内核的前提下把两者接起来：

- [SOUL.md](file:///d:/Miaooaimzzz/xyz_agent/runtime/protoagent/default/config/SOUL.md#L19-L25) 新增「记忆分类」硬规则：偏好 → `memory_ingest(domain="hot", memory_kind="profile", delivery_policy="always")`；决策 → `domain="fact", memory_kind="fact", delivery_policy="retrieved"`；一次性报错不写；敏感信息永不写；同义去重、重申才覆盖。
- 新增技能 `skills/preference-notes/SKILL.md`（26 个技能已索引）：给出分类判据、自包含句写法、去重与覆盖流程、`ERROR:` 时的诚实回退。
- **实测**：一句「记住两件事」→ 模型自主写两条，13.2 秒；库中落为 `('hot','profile','always')` 与 `('fact','fact','retrieved')`，`review_state=pending`。新会话问「按偏好该用什么语言」→ 未调用记忆工具即正确回答（22,095 prompt tokens）；`/api/memory/injections` 记录 `hot_chunk_ids:[1]`，证明偏好每轮注入。
- **保证边界**：偏好写入后仅在上下文预算淘汰中豁免（working state 与 always-on 永不 shed）；若用户手工删除/拒绝该条目，则不再注入。决策走检索，不占每轮预算。
- 测试会话已删除；两条记忆为真实内容（中文回复偏好、`docgen-base:2` 项目决策），保留。

**记忆分类与偏好保护（2026-11-03，方案 B：确定性写入 `pref_guard`）**

方案 A 依赖模型「记得调用 `memory_ingest`」，属于概率路径：模型可能漏写、改写，或在工具循环里反复写。方案 B 用本地规则兜底，不额外调用模型、不消耗 token：

- 新增插件 `runtime/protoagent/default/plugins/pref_guard/`（manifest `version: 0.0.1`，`config_section: pref_guard`，无 secrets、无网络/文件能力），通过 `registry.register_middleware`（ADR 0032）挂到每次模型调用前；知识库走文档化的 `STATE.knowledge_store`（ADR 0031），核心源码零改动。
- 规则：命中稳定偏好触发词（`以后/今后/每次/一律/我习惯/我偏好/我不喜欢/不要再/别再/记住/统一用/默认用…`）且不含一次性标记（`这次/今天/临时/暂时…`）、不是问句（结尾 `?/？`）、不是斜杠命令 → 写入 `domain="hot"`、`memory_kind="profile"`、`delivery_policy="always"`，`heading="用户偏好"`、`subject=pref-<sha1前10位>`、`source="pref_guard"`；内容保存清洗后的用户原句（上限 `max_chars=300`）。
- 去重：写入前扫描 `domain="hot"` 前 100 条，规范化（去空白/标点、小写）后做双向包含判断；重复句直接跳过。
- 可控：控制台 **设置 ▸ Plugins ▸ pref_guard** 提供 `enabled`、`dry_run`、`dedupe`、`max_chars`、`extra_triggers`；异常只告警，绝不影响当轮回复。
- **调试中定位的关键事实**：protoAgent 0.219.0 的 `graph.context_frame` 会把本轮动态上下文（回忆记忆、技能索引、工作状态）作为**独立的、带 `protoagent_injected_context` 标记的 HumanMessage 追加在真实用户消息之后**。首版「取最后一条 Human」拿到的是整个 `<injected_context>` 信封（数千字），被长度上限拦下而未写入。修复：扫描时跳过带该标记的 frame（`_is_context_frame`），并保留整段信封剥离作为兜底。
- **实测（真实模型，2026-11-03 晚）**：
  - 新会话发「以后文档都用 B5 纸张。」→ `chunks` 新增 `(id=6, domain=hot, memory_kind=profile, delivery_policy=always, source=pref_guard)`，内容为用户原句；当轮 `prompt_tokens=44,740`（未新增任何偏好专用模型调用）。
  - 新会话重发同一句 → hot 行数保持 4，未新增（去重生效）；`prompt_tokens=44,759`。
  - 新会话先发一次性指令「这次先用英文输出。」再发问句「以后都这样吗？」→ 两次都不写入，hot 行数保持 4。
  - 清理：`prefguard-a…f / v1…v4` 测试会话经 `DELETE /api/chat/sessions/{id}` 删除（含 digest 与 checkpoint），测试偏好行（`source="pref_guard"`）删除；库中仅保留 3 条真实热记忆（中文回复偏好、`^^` 后缀、称呼「行语中」）。
- **边界**：规则是启发式，宁可漏写（交回方案 A 的模型路径）也不误写一次性指令；偏好与后续显式指令冲突时仍以当前指令为准（SOUL 亦规定重申才覆盖）。

**主模型降级（2026-11-03）**：DeepSeek 会偶发 `503 Service is too busy`（供应商侧过载/限流，非本地故障，`request_id` 由对方返回）。新增 provider `zhipu`（`https://open.bigmodel.cn/api/paas/v4`，复用智谱 key），并设 `routing.fallback_models: [zhipu:glm-4-flash]`：主模型调用失败时自动切备用模型，用户侧不再直接看到 503。已验证 `/api/config` 解析出 `fallback_models=['zhipu:glm-4-flash']`，主链路烟测正常（`prompt_tokens=21,801`，会话 `fbcheck-1103` 已删）。备用模型能力弱于主模型，仅在故障期生效。

**隧道自动恢复（2026-11-02）**：`_create()` 先探测 `domain` 端口；不通则用 `ssh -N -L` 自动重开隧道（`BatchMode=yes`、`ExitOnForwardFailure=yes`、`ServerAliveInterval=30`，Windows 下 `DETACHED_PROCESS` 脱离服务进程存活），默认等待 15s。新增配置 `auto_tunnel` / `ssh_target` / `ssh_remote_host` / `ssh_remote_port` / `tunnel_wait_s`，可在 Settings ▸ Plugins ▸ sbx_tools 关闭或改目标。实测：隧道断开状态下生成 Excel，日志 `reopening ssh tunnel` → `ssh tunnel is up`（1.5s）→ 沙箱创建 → `out/scores.xlsx` 5,105 字节（`PK` 头），PASS。

**生成耗时优化 + 熔断（2026-11-02）**：诊断耗时构成——每个新沙箱都要重装依赖（约 95s），加上推理模型十余轮思考，单次 Excel 生成约 **3.5 分钟**、prompt_tokens 约 19 万。措施：

1. 云端预构建镜像 `docgen-base:1`（预装 python-docx / openpyxl / python-pptx / reportlab），沙箱创建降至 **1.1–1.6s**；`doc-gen` 改为先 `import docx, openpyxl, pptx, reportlab` 探测，已齐则跳过 pip。
2. 插件熔断：连续 3 次创建/连接失败后熔断 60s，期间所有 `sbx_*` **立即返回**且提示“停止重试并告知用户”，不再让模型反复消耗 token；配置 `failure_threshold` / `circuit_cooldown_s`。
3. `max_iterations` 2000 → **100**（约 25 次工具调用；实测设 30 会撞 `GraphRecursionError`），并设 `turn_stall_timeout_seconds: 240` 兜底无输出卡死。

实测：同一 Excel 请求由 **3.5 min → 32.2s**，`out/scores.xlsx` 5,038 字节，PASS。离线熔断测试：关闭端口连续失败 3 次后，第 4 次调用 **0s** 返回 `circuit breaker open for 60s`。

---

## 二、阶段规划（版本号待发布时分配）

| 阶段 | 内容 | 状态 | 依赖 |
|---|---|---|---|
| 阶段一 | 本地运行底座 + 可行性验证 | 已完成（3 项待模型补测） | — |
| 阶段二 | 沙箱工具层 `sbx_*` + 原生文件/执行工具下线 | 已完成（远程 Server 方案） | — |
| 阶段三 | 技能迁移与业务技能落地（正文只引用 `sbx_*`） | 已完成（技能文档 + 运行级 9/9 验证；模型链路待阶段四） | 阶段二验收通过 |
| 阶段四 | 端到端对话验证与交付（真实模型接入、持久化、`load_skill`、工具驱动调用） | 已完成（4/4 实测通过） | — |
| 阶段五 | 拍照问答：视觉理解扩展 `vision_analyze`（**排在最后**） | 已完成（直调 + 模型驱动均通过） | — |

### 阶段三 — 技能迁移与业务技能落地

**目标**：把技能从「宿主机文件工具」迁移到「沙箱工具」，并落第一批业务技能。

**已完成（2026-10-29，待沙箱运行验证）**

| 技能 | 路径 | 说明 |
|---|---|---|
| `sandbox-code-review` | `default/skills/sandbox-code-review/SKILL.md` | 沙箱内上传代码 → 跑 compileall/pytest 或 npm test → 按阻断/建议/提示出结论；明确"不修改宿主仓库"、`ERROR:` 时最多重试一次 |
| `doc-gen` | `default/skills/doc-gen/SKILL.md` | 沙箱内 `pip install python-docx/openpyxl/python-pptx` → 写生成脚本 → 执行 → `sbx_download_file` 取回到 `out\`；无外网时降级为 csv/md/html |

**验证**：重启后日志 `seeded 24 disk skill(s)`、`indexed 24 SKILL.md`；`skills.db` 的 `skills_fts` 共 24 行，`sandbox-code-review`、`doc-gen` 均在册；`/api/tools` 仍为 `count=75`、`sbx_*` 14/14 enabled，原生工具未回潮。产物：`docs/stage1-artifacts/43-tools-after-skills.json`、`44-skills-names-*.txt`、`45-serve-skills2.log`。

**命名踩坑（已修）**：首版命名为 `code-review`，与内置 seed 技能同名（那是 `/code-review` PR 评审斜杠技能），索引里被覆盖、内置技能消失。已改名为 `sandbox-code-review`，两者并存，不复用 `docx/xlsx/pptx/code-review` 等内置名。

**后续**

- `resume-analysis` 等其余业务技能：等视觉能力（阶段五）与沙箱可用后再写。

**运行级验证（2026-11-02，已完成）**：`runtime/stage3-skill-e2e.py` 用真实 `sbx_*` 跑两个技能的完整工作流，**9/9 PASS**：

| 工作流 | 实测 |
|---|---|
| doc-gen：沙箱内 `pip install python-docx` | exit 0（约 290s 内的沙箱复用周期完成） |
| doc-gen：生成 docx | `DOCX_OK`，沙箱内 `/workspace/stage3-report.docx` |
| doc-gen：产物回传 | `sbx_download_file` 取回 **36,782 字节**，宿主文件头 `PK`（合法 docx），路径 `runtime/stage3-out/stage3-report.docx` |
| code-review：`compileall` | `COMPILE_OK` |
| code-review：单元测试 | `TEST_OK`（exit 0） |
| code-review：缺陷检索 | `grep` 命中 `demo.py:2` |
| 生命周期 | create / info（healthy=True）/ destroy 均正常 |

结论：除模型调用外的整条链路（技能 → `sbx_*` → 沙箱 → 产物回传）已证明可用；`pip install` 在 2C2G 服务器上可正常完成，未触发 OOM。

**格式扩展（2026-11-02，已实测）**：`doc-gen` 由「docx」扩到 **docx / xlsx / pptx / pdf / html** 五种，实测 **5/5 PASS**：

| 格式 | 宿主文件 | 字节 | 依赖 |
|---|---|---|---|
| Word | `out/format-demo.docx` | 36,924 | `python-docx` |
| Excel | `out/format-demo.xlsx` | 4,961 | `openpyxl` |
| PPT | `out/format-demo.pptx` | 28,332 | `python-pptx` |
| PDF | `out/format-demo.pdf` | 12,853 | `reportlab` + 中文字体 |
| HTML | `out/format-demo.html` | 387 | 标准库 |

关键修正：

- 沙箱内 `pip install` **必须带清华镜像** `-i https://pypi.tuna.tsinghua.edu.cn/simple`；官方源在 2C2G/跨境网络下 7 分钟都装不完，会被 `timeout_s` 杀掉（首轮实测即此原因失败）。
- PDF 中文字体改为把宿主 `C:\Windows\Fonts\simhei.ttf` 经 `sbx_upload_file` 传进沙箱再 `TTFont` 注册，比 `apt-get install fonts-*` 快得多，且字体会嵌入 PDF。
- 技能正文明确禁止「改扩展名冒充格式」，沙箱不可达时必须直说，不得把 md 说成 docx/pdf。

验收脚本：[docformat-live-test.py](file:///d:/Miaooaimzzz/xyz_agent/runtime/docformat-live-test.py)。

### 阶段四 — 端到端对话验证与交付

**已完成（2026-11-02）**，模型：`deepseek:deepseek-v4-flash`（`https://api.deepseek.com/v1`，key 存 `config/secrets.yaml`，不回显、不进日志与文档）；`max_tokens` 由 32768 下调为 8192（该模型单次输出上限）。

| 验收项 | 实测结果 | 取证 |
|---|---|---|
| 对话基本链路 | `POST /api/chat` → `{"response":"你好呀"}`，prompt_tokens=20871 | `stage4-artifacts/` |
| 模型驱动 `sbx_*` | 模型自主调 `sbx_run_command` 执行 `echo STAGE4-OK`，返回 exit_code 0 与输出 | `05-tool-after-fix.json` |
| `load_skill` 链路 | 加载 `doc-gen` 后能复述“第一步用 `sbx_sandbox_create` 起 `python:3.11-slim` 沙箱” | `01-chat-skill-load.json` |
| 对话持久化（重启后） | 重启服务后同 session 追问上一轮命令，模型准确答出 `echo STAGE4-OK` 与其输出 | `04-followup-after-restart.json` |

**端到端暴露并修复的缺陷**：新建容器内 `/workspace` 不存在，首个 `sbx_run_command` 返回 400 `working directory does not exist`。已在 `_create()` 里创建沙箱后预建 workdir 与 `/tmp/sbx_exec`，复测通过（`FIX-VERIFIED`）。

**已知限制**：`deepseek-v4-flash` 是推理模型，思考过程占 `reasoning_tokens`，`max_tokens` 给小（如 64）会只出 reasoning、`content` 为空；`/v1/models` 返回的官方名是 `deepseek-flash` / `deepseek-v4-pro`，别名可调用但计费按默认费率估算。

### 阶段五 — 拍照问答：视觉理解扩展 `vision_analyze`

**已完成（2026-11-02）**，实现为独立插件 `vision_bridge`（与 `sbx_tools` 平级），端点：智谱 `glm-4v-flash`（`https://open.bigmodel.cn/api/paas/v4`，OpenAI 兼容，key 存 `config/secrets.yaml`）。

工具签名（与草案的差异：`prompt` 改名为 `question`，取消 `detail`，因智谱侧不接受该字段）：

```text
vision_analyze(image, question?, model?)
```

| 参数 | 说明 |
|---|---|
| `image` | 聊天贴图的 media id / 宿主绝对路径 / `http(s)` URL / `data:` URI |
| `question` | 对该图的提问；空则“详细描述 + 全文转写” |
| `model` | 可选覆盖；默认 `glm-4v-flash` |

**缺陷修复：控制台贴图“打不开”（2026-11-02）**

现象：在 `/app` 控制台贴图提问，助手回复“图片我这边打不开”。

根因（取证 `stage4-artifacts/07-user-image-session.json`）：聊天附件不会以路径传给模型，只注入一行引用 `[attached-image refs] … media id: image 1 = 733b781f…`，文件落在 `<instance>/media/<id>.<ext>`；而首版 `vision_analyze` 只认路径 / URL / `data:` URI，解析不了 media id。

修复（`plugins/vision_bridge/`）：

- `_load_image` 解析顺序改为 `data:` URI → `http(s)` URL → **media id（32 位 hex 且命中 media 文件）** → 宿主路径。
- 新增 `_MEDIA_ID` 正则、`_media_dir()`（`PROTOAGENT_BOX_ROOT`/`PROTOAGENT_INSTANCE` 推导，可被配置覆盖）、`_media_file()`（`glob("<id>.*")` 且跳过同名 `.json` 边车）。
- 失败文案带上实际搜索的 media 目录，便于定位。
- manifest 增 `media_dir` 配置项；`photo-qa` 技能第 1 步改为“media id 优先”。

复测（`stage4-artifacts/08-mediaid-e2e.json`）：`/healthz` 200、`/api/tools` count=76 且 `vision_analyze enabled=true`；对话让模型传 `733b781f380140b1a7afc5abab9f92b4` → 模型自主调用 `vision_analyze` → 视觉端点返回 616 tokens 描述 → 助手准确转述图片内容（一张立体几何题截图）。**PASS**。

**体验修正：不再播报“沙箱连不上”等内部状态（2026-11-02）**

现象：看图问答时正文出现“沙箱这会儿连不上，不过这题不需要跑代码，我直接推给你”，属于无关的运维噪音。

根因：模型为了“找图/验算”主动调用 `sbx_find_files` / `sbx_sandbox_info` / `sbx_list_dir`，隧道断开时拿到沙箱错误，又把错误转述给用户。

修复：

- 新增实例级人格文件 `config/SOUL.md`（protoAgent 支持的 persona 位置，不改核心源码）：明确“不播报沙箱/隧道/插件/端点等运行状态；仅在能力真正阻碍任务时用一句话说明并给替代方案”，并规定“看图只走 `vision_analyze`，不探测沙箱”。
- `skills/photo-qa/SKILL.md` 增约束：看图不得调用任何 `sbx_*` 探测工具、不得在回答里提沙箱/隧道状态；`sbx_download_file` 仅在用户明确说图片在沙箱内时使用。

复测（`ux-check` 会话）：重新索引 25 个技能后，模拟贴图提问 → 工具调用只有 `load_skill(photo-qa)` + `vision_analyze(media:31d866ef…)`，无任何 `sbx_*` 调用；回复正文无沙箱/隧道字样，直接给出两问解答。**PASS**。

**新增：回复语言前端可选（中文/英文）（2026-11-02）**

- 新增插件 `plugins/locale/`（manifest `version: 0.0.0`，`config_section: locale`），唯一设置项 `english`（bool），在控制台 **设置 ▸ Plugins ▸ Locale** 显示为「用英文回复（Reply in English）」开关；关闭即简体中文。
- 实现方式：插件 middleware（ADR 0032）在每次模型调用时把语言指令追加到 system message 末尾，优先级高于人格、技能正文与历史消息；核心源码零改动。
- 开关走 `/api/settings`（`layer=agent`）写回 `langgraph-config.yaml` 的 `locale.english`，热重载，`restart_required: []`。
- 复测（`stage4-artifacts/09-locale-toggle.json`）：`english=true` → 英文回复；`english=false` → 中文回复；两个方向都在运行中即时生效，无需重启。**PASS**。
- 踩坑：首版用 content blocks 追加语言段，DeepSeek 的 OpenAI 兼容端点未采纳（回复仍是中文）；改成单一字符串拼接后生效。

**实测**

| 验收项 | 结果 |
|---|---|
| `/api/tools` | `count=76`，`vision_analyze enabled=true` |
| 宿主路径输入 | 返回「左红右蓝」，tokens=83 |
| `data:` URI 输入 | 返回「左边红色、右边蓝色」，tokens=94 |
| 路径不存在 | `ERROR: 读取图片失败 — FileNotFoundError…（沙箱内图片请先 sbx_download_file 取回）` |
| 模型驱动端到端 | 对话「请调用 vision_analyze…」→ 模型自主调用 → 答「左：红色；右：蓝色」 |
| 主模型多模态 | 保持 `model.vision: false`，主模型仍走纯文本 |

**配套技能**：`skills/photo-qa/SKILL.md`（拍照问答），规定“先 `vision_analyze` 转文本、再作答”，并写明与 `sandbox-code-review` / `doc-gen` / 简历分析的衔接路径。

**与沙箱的边界**：沙箱内图片需先 `sbx_download_file` 取回宿主再交给 `vision_analyze`；插件不直连沙箱（避免跨插件持有沙箱句柄）。

**仍待确认**：图片分辨率上限（现为 12 MB 字节上限，未做降采样）；是否要本地 OCR 兜底（当前无，端点不可达直接报错）。

---

## 三、待办与阻塞台账

| 编号 | 事项 | 状态 | 解除条件 |
|---|---|---|---|
| A1 | `sbx_tools` 插件逻辑离线自检（假 Sandbox 驱动 14 个工具，19/19 通过） | 已完成 | — |
| A2 | 阶段二验收脚本 `runtime/verify-sandbox.py` | 已完成（13/13 PASS） | — |
| A3 | 插件级真实调用 `runtime/sbx-plugin-live-test.py`（14 个工具逐个实测） | 已完成（14/14 PASS） | — |
| B1 | 本机 WSL2 不可用（`VirtualMachinePlatform` 因系统组件损坏无法启用） | 已绕过 | 改走远程 Server + SSH 隧道，本机不再需要 Docker |
| B2 | 阶段二运行级验收六项 | 已完成 | 见 `docs/stage-2-verification.md` §4.5 |
| B3 | 真实模型参数（供应商 / base URL / 模型名 / API Key） | 已完成 | DeepSeek `deepseek-v4-flash` 已接入并跑通 4 项验收 |
| B4 | 沙箱隧道：已配好 SSH 免密，但**不再做自启** | 已自动化（2026-11-02） | 插件在 `sbx_*` 调用前探测端口，断了自动重开（`auto_tunnel=true`）；手动命令仍可用：`ssh -N -L 18090:127.0.0.1:8080 root@47.101.71.56` |
| P1 | 阶段三技能清单（迁移哪些、新增哪些） | 已落地首批 | `sandbox-code-review`、`doc-gen` 已完成；其余业务技能待阶段五后 |
| P2 | 阶段五视觉端点拓扑与图片上限 | 已落地 | 端点：智谱 `glm-4v-flash`（免费档），独立插件 `vision_bridge`，key 走 `secrets.yaml`；图片上限暂定 12 MB 字节、未降采样 |
| B5 | DeepSeek 偶发 503（供应商过载） | 已缓解 | `routing.fallback_models=[zhipu:glm-4-flash]` 自动降级；如不想降级可在 config 中移除 |
| P5 | 生成的成品在界面上看不到（Artifact 面板为空） | 已完成（模型级复测：自动登记 `a-1793703479622-8b207d` v1） | `sbx_download_file` 自动调用 `artifact.save_file`；存量文件用 `save_file_artifact` 补登记 |
| P6 | P0 优化（模板化生成、沙箱常驻、成品预览、流式） | 已完成（插件级 3/3 + 模型级端到端均通过） | `docgen-base:2` + `/opt/docgen` 模板脚本；`idle_timeout_s: 0`；运行时补 pptx/xlsx/pdf 预览依赖 |
| P7 | 偏好类消息「永不丢弃」 | 已完成（方案 A，模型自动写入 hot/always 并验证注入） | SOUL 规则 + `preference-notes` 技能；`memory_ingest` 写 `domain="hot"`、`delivery_policy="always"` |
| P8 | 方案 A 的模型概率性漏写/改写偏好 | 已完成（方案 B：`pref_guard` 本地规则写入，实测写入/去重/排除一次性与问句均通过） | 插件 middleware 扫描真实用户消息（跳过 `protoagent_injected_context` frame），写 hot/always；控制台可开关/演练 |
| P3 | 控制台贴图提示“打不开图片”（聊天附件只给 media id，`vision_analyze` 只认路径/URL） | 已修复并复测 | `vision_bridge` 支持 media id 解析；取证 `stage4-artifacts/07-user-image-session.json`（修复前）、`08-mediaid-e2e.json`（修复后 PASS） |
| P4 | 回复语言改为前端可选（中文/英文） | 已落地并复测 | 插件 `locale` 的 `english` 开关（设置 ▸ Plugins ▸ Locale），middleware 每轮注入；热切换 `restart_required: []`，取证 `09-locale-toggle.json` |


---

## 四、变更记录规范

1. **当前开发版本为 `v0.0.1`**：未打 tag 前的改动累加到该版本，按阶段/里程碑分段记录；`v0.0.0` 仅保留阶段一~二的历史内容，不再新增条目。
2. 每次发布追加一节「改动 / 产物 / 验证 / 遗留」，只写**实测通过**的能力；未实测的能力标 `待测`，不得写成已完成。
3. 涉及工具的改动必须附 `/api/tools` 取证 JSON（`docs/stage1-artifacts/`）。
4. 涉及 SDK 的改动必须附 `inspect.signature` 实测输出，禁止按记忆猜 API。
5. 首发后版本号遵循：破坏技能契约或工具改名 → major；新增工具/技能 → minor；修正文档与提示文案 → patch。
