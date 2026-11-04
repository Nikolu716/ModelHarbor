# OpenCode Go

在站点编辑器选择 **OpenCode Go**，新建时默认使用 **Go 订阅**。已有按量配置会如实回填，只有明确保存套餐变更才切换。

- 推理地址：固定为 https://opencode.ai/inference/go/openai/v1。
- 模型目录：https://opencode.ai/inference/go/v1/models；读取目录不生成内容，也不能证明剩余额度或区域权限可用。
- Go 的文本模型支持 Responses、Chat Completions、Anthropic Messages 和 Gemini GenerateContent，协议转换由 OpenCode Console 执行。网关保留客户端协议、工具调用和请求参数，不降级为纯文本。
- 网关只透传明确允许的会话头（x-opencode-session、session-id、thread-id）及真实 User-Agent。兼容 ZCode 原生 x-session-id：缺少前两种会话头时，将其转换为 x-opencode-session，保留真实对话 ID。缺少稳定会话 ID 时在本地返回 400，不发送上游请求、不扣站点健康分。不同会话不会共享静态会话 ID。
- 普通 HTTP 库的 User-Agent 会加上真实网关标识 ModelHarbor/1.0 via，不伪装 OpenCode 客户端。
- Go 禁止通用 ping 生成探测。手动智商测试使用独立测试会话；自动测试仍须用户主动启用。
- 界面显示 Go 订阅及上游额度（5 小时、本周、本订阅周期）；不把缺失余额显示为 $0.00。网关不会把失败的 Go 请求改投该站的余额 API。
- **上游 Extra Usage 是独立设置**：OpenCode 控制台若开启它，Go 超额后仍可能扣按量余额。需要订阅额度用尽即停时，请在该控制台关闭 Extra Usage；网关无法代为读取或修改这一开关。

变更连接或套餐会清除旧连接的配额保护、重新读取目录；历史请求和失败记录保留。日常故障切换只使用支持同一模型和协议的站点。

官方依据（核对日期 2026-11-02）：

- https://opencode.ai/v2/docs/console/go/ ：Where can I use it?、Validated clients、Usage beyond limits、Endpoints。
- https://opencode.ai/v2/docs/console/api/inference ：计费来源、模型能力与返回用量。

回归：tests/test_model_routing.py（模拟推理）、tests/verify_selects_browser.py（隔离浏览器与响应式交互）。

## 订阅用量

GET https://opencode.ai/inference/go/v1/usage 使用站点 Bearer Key 返回 rolling / weekly / monthly 的百分比、状态和重置时间。界面中“总用量”明确指本订阅周期，不代表历史累计 Token，也不是最近请求日志的统计。上游账号数据包含其他客户端的使用。

启用的 Go 站点每 15 分钟读取一次；弹窗打开和页面刷新只读本地缓存。手动刷新至少间隔 60 秒，429 遵守 Retry-After，失败退避并保留上次数据；不发生成请求、不影响健康分、熔断或网关路由限流。停用站点只显示缓存，配置或密钥改变清空旧快照。快照存入原有账号 SQLite，重启保留。

## 智商测试

输出 Token 上限由用户填写，网关只校验正整数，不再限制 8192，不自动截低，也不自动重试。模型不接受时显示上游错误。原有历史结果中没有保存的 HTTP / SSE 原文无法补回；新测试保存有限长度且脱敏的响应原文及明确的结束原因、上游报告的推理 Token。没有可见文本时不生成假预览、不根据总输出猜测推理用量。
