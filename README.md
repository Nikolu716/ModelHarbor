# ModelHarbor

穷人的 LLM 港口，告别中转站抽风。The LLM harbor for the budget-conscious.

OpenAI 兼容的统一入口网关：把多个中转站（relay）的 key 聚合到一个地址，**自动探测健康状态、自动无缝切换故障站点**，解决中转站慢、卡死、401/403/503/限流/容量不足等问题。

客户端（OpenAI SDK / 任何兼容工具）只需要配置一个 base_url，完全无感知。

> **公开仓库安全边界**：真实 `config.yaml`、整个 `data/` 目录、日志和运行状态均为本地私有文件，已被 Git 忽略。请从 `config.example.yaml` 创建本地配置，绝不要把真实 API key、管理密钥或 Webhook 提交到仓库。

## 核心能力

| 能力 | 说明 |
|---|---|
| ✅ 统一入口 | 暴露 `/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`、`/v1/responses`、`/v1/models`，请求/响应原样透传（含 Responses API 流式 SSE） |
| ✅ 自动探测 | 后台循环探测各站 `/v1/models`，维护延迟 EMA 与健康状态 |
| ✅ 故障切换 | 401/403/404/429/5xx/超时/连接失败/**容量不足**（错误信息含 capacity/overloaded/quota 等）自动换下一站，最多尝试 `max_attempts` 个 |
| ✅ 异常自动重试 | 全站暂时不可用时保留原请求，按指数退避等待恢复；支持不限轮次、总时限和退避上限，客户端断开后立即停止 |
| ✅ 计费模式 | 区分**余额制/订阅制**：余额不足(402)、订阅到期(403+关键词)自动标记"配额耗尽"长期停用，充值/续费后手动重置；429 短冷却避免烧配额 |
| ✅ 卡死检测 | 首字节超时（默认 10s）+ 流式块间隔超时（默认 60s），卡死站点被快速跳过并熔断 |
| ✅ 熔断 | 连续探测失败 N 次 → 熔断冷却，冷却期内不路由；真实请求成功立即恢复 |
| ✅ 流式无缝切换 | SSE 流式透传；首字节前可切换，首字节后卡死会向客户端补发错误事件 |
| ✅ 并发控制 | 每站独立并发上限，繁忙站点自动跳过 |
| ✅ 智能路由 | 按 **动态健康评分** 自动排序（延迟 EMA + 近100次成功率 + 状态），无需手工配置优先级，评分相近的站做负载均衡 |
| ✅ 真实链路探测 | 可选 `probe_chat`：极小成本 chat 请求验证"能真正出活"（/models 通不代表生成链路通） |
| ✅ 额度监控 | 自动探测各站余额（credit_grants 接口，不支持则静默降级）；低于阈值黄色预警 + webhook 通知 |
| ✅ 探针可视化 | 每站 60 格红绿热力图（1 格 = 1 分钟，探测+请求结果），故障时段一目了然 |
| ✅ 对外开放 | 统一 base_url + key 鉴权（OpenAI 兼容 401）、fail-fast 智能重试（避免与客户端重试叠加）、SSE 长流保活 |
| ✅ 挂机告警 | 可选 Webhook：站点熔断/恢复/配额耗尽/低余额/手动重置时推送（企业微信/钉钉/Server酱兼容） |
| ✅ 管理 | Web 仪表盘（实时评分/排名/状态/站点管理/重试设置/日志）+ CLI 命令 |
| ✅ 统计 | 调用次数/成功率/切换次数/延迟，JSON 落盘，重启不丢 |

## 快速开始

### 环境（推荐 miniforge3 / conda）

```bash
# 1. 获取依赖环境（Python 3.10+）
conda create -n zgateway python=3.12 -y
conda run -n zgateway pip install -r requirements.txt

# 2. 创建本地配置；真实文件不会被提交
cp config.example.yaml config.yaml
# 编辑 config.yaml，填入你的中转站 URL、模型和 API key

# 3. 启动网关（以下命令在 zgateway 环境下执行）
python gateway.py                      # 示例配置默认监听 127.0.0.1:8000
python gateway.py --port 9000          # 自定义端口

# 4. 客户端接入（如启用 api_keys，必须使用其中一个 key）
#   base_url = http://127.0.0.1:8000/v1
#   api_key  = your-gateway-key
```

首次运行前必须把 `config.example.yaml` 复制为 `config.yaml` 并填写至少一个真实上游。网关会在 `data/` 中生成站点持久化配置、重试覆盖、统计和日志；这些文件只应留在部署主机。

## 对外开放（统一 base_url + key）

架构：客户端（Codex/agent/SDK）只连接网关的统一地址，网关后台自动调度多个中转站，任何单站故障对客户端无感。

```yaml
gateway:
  # 对外统一 key（必配！建议 openssl rand -hex 32 生成）
  # 客户端请求必须带 Authorization: Bearer <key>；留空 = 不鉴权（仅内网）
  api_keys: ["replace-with-gateway-key"]
  # 管理接口 key：配置后 /admin/* 数据接口需带该 key，仪表盘会提示输入
  admin_key: "admin-你的管理key"
  # SSE 长流保活间隔（秒）：定期发注释行防止 Codex 等客户端空闲超时（默认 60）
  sse_keepalive_interval: 60
```

**智能重试策略（瞬时故障等待恢复，确定性错误快速返回）**：
- OpenCode 主会话对最终 5xx 最多再重试 5 次，因此网关的单请求等待默认限制为 60 秒
- 网关只在**存在瞬时失败**（503/429/超时/卡死/容量/冷却/并发已满）时保留原请求并继续重试
- **确定性失败**（401/403/404/400/422 等——重试不会变好）快速返回，不浪费重试预算
- `max_rounds: 0` 表示不限轮次，但必须配置有限的 `max_retry_seconds`；客户端断开会立即取消等待和上游任务
- 重试参数可在仪表盘的“重试设置”Tab 修改，保存后立即生效并写入 `data/gateway-settings.json`

**安全建议（对外开放时）**：
- 必须 HTTPS（nginx/caddy 反代一行配置，或 uvicorn --ssl-keyfile/--ssl-certfile），否则 key 明文传输会泄露
- 网关进程建议服务化（Windows NSSM / Linux systemd），崩溃自动重启
- 401 响应为 OpenAI 兼容格式（SDK 对 401 不重试）

### Windows 服务化（开机自启 + 崩溃自动重启）

项目自带 NSSM（`tools/nssm-2.24/`）和可移植 PowerShell 安装器。公开仓库不会提供真实配置；安装器首次运行时会从 `config.example.yaml` 创建 `config.yaml`，请先编辑该文件并填写上游信息。

```powershell
# 以管理员身份在项目目录执行
Set-ExecutionPolicy -Scope Process Bypass
.\Install-ZGateway.ps1

# 验证服务、健康页和本地配置
.\Verify-ZGateway.ps1

# 卸载服务注册（保留项目文件和配置）
.\Uninstall-ZGateway.ps1
```

安装器会创建项目内 `.venv`、安装 `requirements.txt`、注册 `ZGateway` 服务、设置崩溃重启、注册五分钟 watchdog，并写入 `data/service.log` 与 `data/service.err.log`。Python 3.10+ 和依赖下载或本地镜像是前置条件。`tools/install.cmd` 只是调用该 PowerShell 安装器，不依赖固定的 Python 安装路径。

### 系统托盘管理（右下角小图标，轻量 ~30MB）

托盘应用把网关管理放进 Windows 右下角：

```
托盘图标（黑白 ZGateway 几何路由标识，双击=打开管理面板）
├── 🌐 打开管理面板      ← 双击也可（浏览器打开仪表盘）
├── 📊 服务状态          ← 气泡提示：服务运行状态 + 网关在线状态
├── 服务管理
│   ├── 🔄 重启服务
│   ├── ⏹ 停止服务
│   └── ▶️ 启动服务
├── 🖥 开机自启           ← 勾选/取消（注册表 HKCU Run）
└── 🚪 退出托盘
```

- 启动：`.venv\Scripts\pythonw.exe tools\tray.py`（pythonw 无控制台窗口）
- **内存占用 ~28MB**，不加载任何项目模块（纯 pystray + 系统命令），单实例运行
- 开机自启已注册（随系统启动自动出现在托盘）
- 桌面快捷方式"中转站网关管理"可直接打开管理面板
- 依赖：`pip install pystray pillow`（已装入 zgateway 环境）
- 网页 favicon、页头和 Windows 托盘共用 `app/web/static/brand-mark.svg` 母版；运行 `python tools/build_brand_assets.py` 可重新生成 PNG/ICO

> 其他 Python 3.10+ 环境同样可用（venv/uv 均可），依赖见 `requirements.txt`。

## 配置文件说明

```yaml
gateway:
  host: 0.0.0.0
  port: 8000
  max_attempts: 3          # 单请求最多尝试的中转站数
  retry_backoff: 0.3       # 切换站点之间的等待秒数
  max_rounds: 0            # 0 = 不限轮次；必须搭配有限的 max_retry_seconds
  round_backoff: 1.0       # 轮间基础退避秒数（第 N 轮前等待 round_backoff * 2^(N-1)）
  max_round_backoff: 10    # 轮间退避上限
  max_retry_seconds: 60    # 单请求整体重试总时限；有限轮次时可设为 0
  balance_probe_interval: 300   # 余额探测间隔（秒，GET 免费）
  balance_alert_threshold: 1.0  # 余额低于该值(美元)黄色预警 + webhook 通知
  probe_interval: 15       # 默认健康探测间隔（秒）
  fail_threshold: 3        # 连续失败多少次后熔断该站
  cool_down: 60            # 熔断冷却秒数，结束后自动重新探测
  connect_timeout: 10      # 连接超时
  first_byte_timeout: 15   # 首字节超时（卡死检测）
  read_timeout: 600        # 流式块间隔超时（深度推理需留足静默时间）
  data_dir: data           # 统计落盘目录

stations:
  - name: station-a        # 唯一名称
    base_url: https://api.xxx.com/v1   # 中转站地址（写不写 /v1 都行）
    api_key: replace-with-upstream-key
    models: [gpt-4o, gpt-4o-mini]      # 静态声明支持的模型（可选）
    priority: 1            # 越小越优先（默认 5）
    weight: 1.0            # 同优先级下负载权重（默认 1.0）
    timeout: 300           # 非流式总超时（默认 300）
    max_concurrency: 20    # 该站最大并发（默认 20）
    probe_models: true     # 额外探测 /v1/models 合并模型列表（默认 false）
    probe_path: /models    # 探测路径（默认 /models）
    probe_interval: 15     # 该站探测间隔（默认继承 gateway）
    billing: balance       # 计费模式: balance(余额制,默认) / subscription(订阅制)
    headers: {}            # 附加请求头（可选）
    enabled: true
```

## 动态托管模式（挂机全自动）

默认开启，**无需手工配置优先级**——系统根据每个站点的实时健康自动排列路由顺序：

```
健康评分(0-100) = 延迟分量(0-40) + 近100次成功率(0-30) + 手动偏好加成(0-15)
                 - 熔断惩罚(35) - 配额耗尽惩罚(60)
```

- **自动排序**：评分高的站优先路由；评分相近(±5)的站做加权随机负载均衡，避免频繁抖动
- **自动切换**：慢/故障/卡死站点在请求中被跳过，自动换下一个候选
- **异常自动重试**：全部站点暂时不可用时，原请求按 `round_backoff` 指数退避并受 `max_round_backoff` 封顶；默认不限轮次、最多等待 60 秒，恢复的站立即参与下一轮
- **自动熔断**：连续探测失败 N 次 → 冷却期排除 → 冷却结束自动重新探测
- **自动恢复**：探测成功或真实请求成功 → 状态回升 → **自动重新加入路由**（无需人工干预）
- **真实链路探测**（可选）：`probe_chat: true` 定期发极小 chat 请求，验证 /models 正常但生成链路实际 503 之类的问题

仪表盘提供两种视图（默认监控列表，可切换卡片）：
- **监控列表**：每站一行紧凑展示排名、状态、计费、评分、延迟、成功率、调用、额度、探测、最近错误和操作；窄屏自动隐藏低优先级列并保留横向滚动
- **卡片视图**：完整信息卡片（评分条、模型列表、额度、配额提示），适合窄屏查看与操作

**探针热力图**：每站 60 格历史（1 格 = 1 分钟，记录探测和请求结果），区分正常、部分异常、故障和无数据；悬停可看每分钟成功/失败次数。

**额度监控**：自动查询各站 `credit_grants` 余额接口（OpenAI/one-api/new-api 兼容，不支持则显示"不可用"不报错）。余额低于 `balance_alert_threshold`（默认 $1）时：列表黄色预警 + webhook `balance_low` 通知，提醒充值。

CLI `status` 同样展示实时评分与动态排名（`#1 #2 ...`）及额度。

## 挂机告警（Webhook）

配置 `gateway.webhook_url` 后启用（留空关闭），状态变化自动推送，5 分钟同事件去重：

```yaml
gateway:
  webhook_url: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"  # 企业微信机器人
  # 或钉钉机器人 / Server酱 https://sctapi.ftqq.com/xxx.send / 任意自定义端点
```

推送 JSON：`{"event": "station_down|station_up|station_quota|station_reset", "station": "...", "message": "...", "ts": ...}`

典型场景：挂机时站点余额耗尽/订阅到期/全线故障，微信/钉钉立即收到通知；站点自动恢复也会通知，全程无需盯屏。

## 计费模式：余额制 vs 订阅制

中转站的计费方式不同，失败处理策略也不同，网关会按 `billing` 字段区分对待：

| 维度 | 余额制 balance | 订阅制 subscription |
|---|---|---|
| 计费特征 | 预充值按量扣费 | 月费/套餐，有 RPM/额度限制 |
| 钱的问题 | `402` / `insufficient_quota` / `余额不足` / `欠费` | `403` + `subscription expired/inactive` / `套餐到期` / `未续费` |
| 处理 | 标记 **配额耗尽**：从路由长期排除，充值后手动重置 | 标记 **配额耗尽**：从路由长期排除，续费后手动重置 |
| 429 限流 | 短冷却 `rate_limit_cooldown`（默认 30s）后自动恢复 | 同左——尤其重要：避免重试风暴烧掉订阅配额 |
| 恢复方式 | 充值后 `reset`（重置会立即探测确认，钱未到账会再次标记） | 续费后 `reset`（同上） |

要点：

- **配额耗尽 ≠ 熔断**：熔断是临时故障（自动恢复）；配额耗尽是需要人工干预的资金问题，**探测成功也不会自动清除**（探测只证明服务活着，不证明有钱）
- **重置确认机制**：`reset` 后网关立即探测，若仍返回配额错误会再次标记，防止钱未到账误以为恢复
- **普通 401/403**（如 key 错误、权限禁止）不涉及资金，按普通可切换故障处理

管理入口：Web 仪表盘（配额耗尽卡片显示 💰 提示与"充值/续费后重置"按钮）、`python cli.py reset <站点>`、`POST /admin/station/{name}/reset`。

> **模型路由规则**：请求的模型会匹配所有支持该模型且健康的站点。`models` 未声明的站点接受任意模型；`probe_models: true` 会自动探测站点模型列表并合并。

## 客户端接入示例

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="any",        # 网关不校验 key
)
# 流式 / 非流式用法与官方完全一致，故障切换在网关内部透明完成
```

### Codex CLI 接入

在 Codex 配置中把 `base_url` 指向网关即可（`wire_api` 保持不变，网关透传 `/v1/responses`）：

```toml
model_provider = "OpenAI"
model = "gpt-5.5"
disable_response_storage = true

[model_providers.OpenAI]
name = "OpenAI"
base_url = "http://127.0.0.1:8000/v1"   # 指向网关，而非中转站
wire_api = "responses"
requires_openai_auth = false            # 网关不校验 key

[features]
goals = true
```

> 网关支持 Responses API（`/v1/responses`，含流式 SSE 事件透传），Codex 走 `wire_api = "responses"` 时通过网关即可自动获得多站故障切换。`store: false` 等参数原样透传。

## 管理

### Web 仪表盘
浏览器打开 `http://127.0.0.1:8000/dashboard`
- 各站点状态灯（在线/熔断/冷却/禁用）、延迟、成功率、调用数、模型列表
- 一键启用/禁用、立即探测
- 今日汇总、24 小时时间线，以及“站点 Tab + 单层模型 Token 饼图 + 全站 Token 横向对比”
- 图表只统计上游响应明确返回的 `usage`；同时显示 Token 回传覆盖调用数，未返回时不会用调用量估算
- 全局路由状态条区分“正在使用”和“最近使用”，并在并发时列出活跃站点/模型
- 最近事件独立 Tab（成功/失败/切换），服务端分页且最多保留最新 500 条
- 单条事件详情最多保存 2048 字符；聚合统计保留最近 31 天，单站每日模型超限后合并为“其他模型”
- 桌面、平板和手机宽度均提供响应式布局

看板静态结构、旧统计数据兼容性和运行中接口可通过以下命令验证：

```bash
# Python 单元测试（不访问真实上游）
python -m unittest discover -s tests -p "test_*.py" -v

# Dashboard DOM/XSS 回归测试
npm ci --prefix tests
node tests/verify_dashboard_ui.js

# 可选：启动本地 mock 后做在线看板检查
python tests/verify_dashboard.py --live
```

### CLI

```bash
python cli.py status                    # 查看所有站点状态
python cli.py probe station-a           # 立即探测某站
python cli.py enable station-a          # 启用某站
python cli.py disable station-a         # 禁用某站
python cli.py reset station-a           # 充值/续费后重置配额耗尽状态
python cli.py log --tail 50             # 查看最近事件
python cli.py ping                      # 检查网关连通性
python cli.py --host http://192.168.1.5:8000 status   # 指定网关地址
```

### Admin API

| 接口 | 说明 |
|---|---|
| `GET /admin/status` | 实时站点状态、聚合统计与当前/最近路由活动 |
| `GET /admin/activity/stream` | 当前路由活动的 SSE 实时推送 |
| `GET /admin/stats/daily` | 今日按站点/模型的调用、Token、Token 回传覆盖与时间线 |
| `POST /admin/station/{name}/toggle` | 启用/禁用 |
| `POST /admin/station/{name}/probe` | 立即探测 |
| `POST /admin/station/{name}/reset` | 充值/续费后重置配额耗尽状态（立即探测确认） |
| `GET /admin/logs?page=1&page_size=20` | 最新优先的事件分页（最多保留 500 条） |
| `GET /admin/logs?tail=50` | 事件日志兼容查询（最新优先） |

## 故障转移策略

**可切换（自动换下一站）**：401（key 失效）、403、404（模型不存在）、429（限流）、408、5xx、连接失败、超时、卡死，以及错误信息含 `capacity / overloaded / insufficient / quota / 无余量 / 负载` 等容量关键词。

**配额耗尽（长期停用，需人工重置）**：402、`insufficient_quota`、`余额不足`、`欠费`（余额制）；`403 + subscription expired/inactive`、`套餐到期`、`未续费`（订阅制）。从路由长期排除，充值/续费后 `cli.py reset` 或仪表盘按钮恢复。

**429 限流**：标记短冷却（`rate_limit_cooldown`，默认 30s），冷却期内不路由该站，避免重试风暴。

**直接返回（不重试）**：400、422 等客户端请求本身的问题，重试其他站无意义。

**流式请求**：首字节到达前可无缝切换；首字节后上游中断无法换站（SSE 协议限制），网关会向客户端补发一个错误事件，并标记该站故障。

## 本地测试（模拟中转站）

项目自带 mock 上游，可模拟正常/慢/卡死/401/503/容量不足/随机失败：

```bash
# 终端1: 起 mock（正常、慢3s、卡死、401、503容量、402余额不足、订阅到期、请求429）
python tests/mock_upstream.py --port 9101 --name good
python tests/mock_upstream.py --port 9102 --name slow --delay 3
python tests/mock_upstream.py --port 9103 --name stuck --stuck
python tests/mock_upstream.py --port 9104 --name bad401 --error 401
python tests/mock_upstream.py --port 9105 --name busy --capacity
python tests/mock_upstream.py --port 9106 --name noquota --error 402
python tests/mock_upstream.py --port 9107 --name subexpired --sub-expired
python tests/mock_upstream.py --port 9108 --name ratelimit --rl-requests

# 终端2: 用测试配置启动网关（快熔断参数，便于观察）
python gateway.py --config tests/gateway-test.yaml

# 终端3: 验证
curl http://127.0.0.1:8000/v1/models
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mock-model","messages":[{"role":"user","content":"hi"}]}'
python cli.py status        # 观察熔断/切换
```

mock 参数：`--error 401|402|403|404|429|500|502|503|504` 固定错误码、`--capacity` 容量不足、`--sub-expired` 订阅到期(403)、`--rl-requests` 探测正常但请求 429、`--delay N` 慢响应、`--stuck` 永不响应、`--fail-rate 0.5` 随机失败、`--stream-delay N` 流式块间隔。

## 目录结构

```
gateway.py          # 启动入口
cli.py              # CLI 工具
config.example.yaml   # 安全的配置模板（复制为本地 config.yaml 后编辑）
config.yaml           # 本地真实配置（不要提交）
data/                # 本地运行状态、统计和日志（不要提交）
app/
  config.py         # 配置加载校验
  errors.py         # 错误分类（可切换 vs 直接返回）
  health.py         # 健康探测与熔断
  router.py         # 候选排序与并发控制
  proxy.py          # OpenAI 转发 + SSE 透传 + 故障转移
  state.py          # 站点运行时状态（健康/延迟/统计）
  stats.py          # 事件日志与 JSON 落盘
  web/admin.py      # 管理 API
  web/static/       # 仪表盘前端
tests/
  mock_upstream.py  # 模拟中转站
  gateway-test.yaml # 测试配置
```

## 常见问题

- **所有站都失败返回 502**：聚合错误信息里会列出每个站的具体失败原因，据此排查（key 失效？容量？卡死？）
- **流式中途断开**：SSE 协议无法中途换站，网关会补发错误事件并记录该站故障；客户端可自行重试
- **统计落盘**：默认每 30 秒写 `data/stats.json`，重启自动恢复；事件、详情长度、统计天数和模型桶均有硬上限
- **Token 口径**：非流式响应读取 `usage`（Responses 也兼容 `response.usage`）；流式 SSE 使用带缓冲解析器，只有明确回传的 Token 才进入图表。旧版整数 Token 数据可继续读取，但其历史覆盖率会标为未知。
- **入口鉴权**：示例模板默认不校验 key（仅本机 mock 使用）。生产部署应在 `config.yaml` 设置随机 `api_keys` 和 `admin_key`，并限制网络访问。
