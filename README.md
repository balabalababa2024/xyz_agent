# xyz_agent — 本地运行的智能助手

基于 [protoAgent](https://pypi.org/project/protolabs-agent/) 0.219.0 搭建的本地 Agent：自带 Web 控制台、隔离沙箱执行、文档生成、图片问答和长期偏好记忆。**不修改 protoAgent 核心源码**，全部能力通过插件、配置与技能实现。

## 功能

- **对话控制台**：`http://127.0.0.1:7870/app`，支持中英文切换、会话历史、Artifact 成品面板。
- **隔离执行**：读写文件、跑命令、执行代码全部走 OpenSandbox 沙箱（原生 `run_command` / `write_file` 等已禁用）。
- **文档生成**：Word / Excel / PPT / PDF / HTML，固定模板脚本一次成型，成品自动登记到 Artifact。
- **图片问答**：聊天贴图或给路径/URL，由视觉模型转文字后作答。
- **长期记忆**：稳定偏好自动写入热记忆并每轮注入；决策/事实按需检索；一次性报错不落库。
- **故障降级**：主模型报错时自动切备用模型。

## 架构

| 层 | 组成 |
|---|---|
| 接入 | React 控制台 + HTTP API（`/api/chat`、`/api/tools`、`/healthz`） |
| 运行时 | protoAgent 0.219.0 + LangGraph Agent + SQLite 持久化 |
| 插件 | `sbx_tools`（沙箱工具）、`vision_bridge`（视觉）、`pref_guard`（偏好守卫）、`locale`（语言） |
| 执行 | 远程 OpenSandbox Server（Docker），本机经 SSH 隧道访问 |
| 模型 | DeepSeek（主）、智谱 GLM（降级 / 视觉） |

## 目录结构

```text
docs/version-log.md                 版本与变更记录
runtime/
  sandbox.toml                      OpenSandbox Server 配置示例
  tunnel-up.ps1                     SSH 隧道守护脚本（需改目标地址）
  templates/docgen/                 文档模板脚本 + 镜像 Dockerfile
  protoagent/
    host-config.yaml                模型默认值
    default/
      config/
        SOUL.md                     人格与记忆规则
        langgraph-config.yaml       运行时配置（模型/插件/沙箱/技能）
        secrets.example.yaml        密钥模板（复制为 secrets.yaml）
      plugins/                      sbx_tools / vision_bridge / pref_guard / locale
      skills/                       doc-gen / photo-qa / preference-notes / sandbox-code-review
```

运行时状态（`*.db`、`memory/`、`media/`、`artifact/`、日志）不入库，首次启动自动创建。

## 前置条件

- Python 3.11 与 [uv](https://docs.astral.sh/uv/)
- `protolabs-agent 0.219.0`
- 可选：一台能跑 Docker 的 Linux 主机（部署 OpenSandbox Server）。没有沙箱时只能纯对话，**文档生成与代码执行不可用**。

## 快速开始

### 1. 安装运行时

```bash
uv tool install --force ^
  --with "opensandbox==1.2.0" ^
  --with "python-pptx" --with "openpyxl" --with "reportlab" ^
  "protolabs-agent==0.219.0"
```

Linux/macOS 把 `^` 换成 `\`。`opensandbox` 是沙箱 SDK，`python-pptx/openpyxl/reportlab` 供控制台预览成品；纯聊天可只装 `protolabs-agent`。

### 2. 配置密钥

```bash
cp runtime/protoagent/default/config/secrets.example.yaml runtime/protoagent/default/config/secrets.yaml
```

按需填写：

```yaml
providers:
  deepseek: "<DeepSeek API Key>"   # 主模型
  zhipu: "<智谱 API Key>"          # 降级模型 + 视觉
vision_bridge:
  api_key: "<智谱 API Key>"
```

也可以跳过这一步，首次启动后在控制台设置向导 / **设置 ▸ 模型** 里填写，系统会自动写入同一文件。`secrets.yaml` 已被 `.gitignore` 排除，不会进入仓库。

### 3. 启动

Windows PowerShell：

```powershell
$env:PROTOAGENT_BOX_ROOT = Join-Path $PWD "runtime\protoagent"
$env:PROTOAGENT_INSTANCE = "default"
protoagent serve
```

Linux/macOS：

```bash
export PROTOAGENT_BOX_ROOT="$PWD/runtime/protoagent"
export PROTOAGENT_INSTANCE=default
protoagent serve
```

启动约 35–40 秒（LangGraph 图编译占大头），看到 `Uvicorn running on http://127.0.0.1:7870` 即就绪。打开 <http://127.0.0.1:7870/app>，或检查：

```bash
curl http://127.0.0.1:7870/healthz
```

首次启动会进入设置向导（仓库不包含 `.setup-complete`）；若未出现，直接到 **设置** 里补全模型与密钥。

## 沙箱配置（可选，但文档/代码能力依赖它）

### 方案 A：远程 OpenSandbox Server（推荐）

在 Linux 服务器上（Ubuntu 20.04+ / Debian 11+，2C2G 起，需 Docker）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install opensandbox-server
opensandbox-server init-config ~/.sandbox.toml --example docker
# 确认 [server] host = 127.0.0.1，端口记下来（示例 8090）
OPENSANDBOX_INSECURE_SERVER=YES nohup opensandbox-server --config ~/.sandbox.toml > ~/sandbox.log 2>&1 &
```

本机建立隧道并保持不关（把地址换成你的服务器）：

```powershell
ssh -N -L 18090:127.0.0.1:8080 root@<your-server>
```

也可以编辑 `runtime/tunnel-up.ps1` 里的 `$target` / `$forward`，让它断线自动重连。

然后在 **设置 ▸ Plugins ▸ sbx_tools** 或 `runtime/protoagent/default/plugins/sbx_tools/protoagent.plugin.yaml` 中改：

| 配置 | 含义 |
|---|---|
| `domain` | 本机隧道入口，默认 `localhost:18090` |
| `ssh_target` | 你的服务器 `user@host`（仓库里是作者环境，**必须改**） |
| `ssh_remote_port` | 服务器上 opensandbox-server 的端口（示例 `8080`） |
| `server_proxy` | 跨网络访问必须为 `true`（隧道只转发 API 端口） |
| `auto_tunnel` | 隧道断开时自动重连 |
| `image` | 沙箱镜像，默认 `docgen-base:2` |
| `idle_timeout_s` | `0` = 常驻，不按空闲回收 |

`runtime/sandbox.toml` 是 Server 端配置示例：`[store] path` 要改成服务器上的实际路径，端口按需修改。

### 方案 B：本机/同机 Server

在 Linux 或支持 Docker 的机器上直接跑 `opensandbox-server`，把 `domain` 改为 `localhost:8090`（或实际端口），`auto_tunnel` 设为 `false`。

> 强烈建议只监听 `127.0.0.1` 并通过隧道访问；`OPENSANDBOX_INSECURE_SERVER=YES` 表示**无鉴权**，不要把端口暴露到公网。

### 文档生成镜像

`docgen-base:2` 预装了 `python-docx / openpyxl / python-pptx / reportlab`，并在 `/opt/docgen/` 内置模板脚本。在沙箱主机上构建：

```bash
cat > Dockerfile.base <<'EOF'
FROM python:3.11-slim
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    python-docx openpyxl python-pptx reportlab
EOF
docker build -f Dockerfile.base -t docgen-base:1 .
cd runtime/templates/docgen && docker build -t docgen-base:2 .
```

不想预构建时，把 `image` 改成 `python:3.11-slim`，首次生成会现场安装依赖（更慢，且需要沙箱能出网）。

## 关键配置

| 文件 / 配置项 | 作用 |
|---|---|
| `config/langgraph-config.yaml → model.name` | 主模型，默认 `deepseek:deepseek-v4-flash` |
| `→ routing.fallback_models` | 主模型失败时的备用模型，默认 `zhipu:glm-4-flash` |
| `→ providers` | 模型连接列表（`deepseek` / `zhipu`），密钥来自 `secrets.yaml` |
| `→ tools.disabled` | 禁用的原生文件/命令工具（全部由 `sbx_*` 替代） |
| `→ sbx_tools` | 沙箱地址、镜像、超时、常驻策略 |
| `→ locale.english` | 回复语言（也可在控制台切换） |
| `config/SOUL.md` | 人格、记忆分类规则、禁止播报运行状态 |
| `plugins/pref_guard/` | 偏好确定性写入 hot 记忆（可在设置里开关 / dry-run） |
| `plugins/vision_bridge/` | 视觉模型端点、模型名、密钥 |

换模型：改 `providers` 里的 `base_url` 与 `model.name`，并保证连接 id 与 `secrets.yaml` 的键名一致。只用一家供应商时，可删掉备用模型与视觉配置。

## 验证清单

1. `curl http://127.0.0.1:7870/healthz` 返回 `200`。
2. 控制台能正常对话、能切换中英文。
3. 问「帮我生成一份 Excel」：沙箱创建 → 生成 → 下载 → Artifact 面板可见成品。
4. 问「帮我跑一段 Python」：工具调用应全部是 `sbx_*`。
5. 发「以后文档都用 B5 纸张」：无需模型记忆也能在下一轮生效（热记忆注入）。

## 常见问题

- **启动很慢 / 以为卡住**：图编译约 30–40 秒，属正常；期间访问会失败。
- **端口 7870 被占用**：先结束旧进程再启动，避免两个实例共用 `checkpoints.db`。
- **沙箱显示 not_created**：这是常态，首次调用工具时才惰性创建。
- **文档生成报“环境连不上”**：隧道断了。检查 `ssh -L` 进程、`localhost:18090` 端口，或依赖 `auto_tunnel` 自愈。
- **503 / 模型过载**：确认 `routing.fallback_models` 与备用模型的密钥可用。
- **模型报鉴权失败**：`secrets.yaml` 缺失或键名与 `providers` 的 `id` 不一致。
- **生成的文件是 Markdown 而不是目标格式**：通常是沙箱不可用导致的降级；修好沙箱后重试。

## 安全须知

- **永远不要提交 `secrets.yaml`**（`.gitignore` 已排除；`secrets.example.yaml` 只含空值）。
- 如果密钥曾经进过聊天记录、截图或外部日志，请到供应商后台轮换。
- 沙箱服务只监听 `127.0.0.1`，通过 SSH 隧道访问；不要公网开放无鉴权端口。
- 仓库里的 `runtime/tunnel-up.ps1` 与沙箱插件默认地址是作者环境，请替换成你自己的。

## 版本

版本与变更记录见 [docs/version-log.md](docs/version-log.md)，当前开发版本 `v0.0.1`。
