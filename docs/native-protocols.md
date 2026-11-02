# 原生生成协议

ModelHarbor 在原有地址和端口上提供以下入口：

| 协议 | 入口 |
| --- | --- |
| OpenAI Responses | POST /v1/responses |
| OpenAI Chat | POST /v1/chat/completions |
| Anthropic Messages | POST /v1/messages |
| Gemini GenerateContent | POST /v1beta/models/{model}:generateContent |
| Gemini 流式 | POST /v1beta/models/{model}:streamGenerateContent?alt=sse |

Gemini 同时接受 /v1/models/{model}:… 前缀。不传 alt=sse 时按原生格式返回 JSON 数组。模型目录为 GET /v1/models 和 GET /v1beta/models。

站点编辑器可选 Anthropic Messages 或 Google Gemini，保存上游地址及密钥后读取模型目录。每个站点使用自己的模型范围、路由与探测配置；模型目录不代表额度或区域权限已经可用。默认不新增自动生成探测。

客户端可用 Authorization: Bearer，也可按协议使用 x-api-key（Messages）或 x-goog-api-key（Gemini），其值是网关密钥。不要将密钥放进 URL。网关向上游使用站点密钥，透传允许的 anthropic-version、anthropic-beta 和稳定会话信息。账号、IP 白名单、并发及请求日志仍沿用同一套管理规则。

普通原生站点按所选协议路由，不会自动成为 OpenAI 接口。OpenCode Go 的文本模型转换由 Console 完成，网关保留原始工具、多模态和会话字段，固定使用 Go 入口；不会失败后改走该站余额入口。

智商测试支持手动选择以上生成协议。自动模式根据模型目录选择；原生推理参数遵循对应模型的 thinking / thinkingConfig，不套用 OpenAI 的推理等级。原生模式的推理选项为自动继承或不发送。

流式原样透传，保活不会取消上游读取；没有完整结束事件或中途错误会标记失败。Anthropic 输入统计包含缓存读写，Gemini 输出统计包含思考 Token。未返回用量时不猜测费用。

本次支持核心生成与模型目录，不包括 Files、Batch、Realtime、原生文件上传或任意协议之间的本地转换。

官方参考（核对日期 2026-11-02）：

- https://platform.claude.com/docs/en/api/messages
- https://ai.google.dev/api/generate-content
- https://opencode.ai/v2/docs/console/go/
- https://opencode.ai/v2/docs/console/api/inference

验证：tests/test_native_protocols.py 使用模拟传输覆盖请求完整性、鉴权、目录分页、流式工具与用量、慢流保活和中断处理；无外部生成请求。
