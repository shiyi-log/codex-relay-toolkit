# 对标调研与路线图

调研时间：2026-10-07。对象：`MetaFARS/codex-relay`、`hugalafutro/model-hotel`、
`thezillo/codex-proxy`、`ImBIOS/relay`、`BerriAI/LiteLLM`。
方法：拉源码逐文件核对（`git clone` + curl；本机 `web_fetch` 对这些域名被解析成非公网 IP 而失败），
所以下面每条结论都有文件/行号依据，不是 README 摘要。

## 一句话结论

**我们的差异化是别人没有的**：`model-hotel` 的排序是**纯人工优先级表**（价格只用于展示），
LiteLLM 的 `cost-based-routing` 用**静态价格表**、延迟用**全局平均**——两者都不做
"逐中转实测真实单价 + 实测首字节 + 失败率 → 每次请求重排"。
**我们的差距**集中在：协议转换、熔断/错误分类、流式健康、订阅账号 OAuth 与配额。

## 五个项目核实结果

| 项目 | 描述是否属实 | 实况 | 与我们的关系 | 最值得借鉴 |
|---|---|---|---|---|
| [MetaFARS/codex-relay](https://github.com/MetaFARS/codex-relay) | ✅ | Rust/MIT，187★，13k 行；**只做 Responses→Chat Completions 单向翻译**，支持 DeepSeek/Kimi/Qwen/GLM/Mistral/Groq/xAI/OpenRouter；**没有任何重试/故障转移**（全仓只有一处 "retry" 注释） | **互补而非竞争**：它缺的正是我们有的（重试/轮询/评分），我们缺的正是它有的（协议转换） | quirk 注册表（按模型门控的请求整形 + 按异常触发、可自愈的响应修复 + 移除遥测）；`_UPSTREAM_EXTRA_PARAMS`/`_DROP_PARAMS` 逃生阀；按 Codex CLI 版本固定的 fixture 回放测试；日志只记工具名不记参数 |
| [hugalafutro/model-hotel](https://github.com/hugalafutro/model-hotel) | ✅ | Go+React+PG，56★，每天提交；**熔断按 (provider, model)**，TTFT 探针先缓冲不提交，流停滞看门狗 | 功能重叠但方向不同：它有大量我们该学的稳定性机制，**没有价格/延迟路由** | ①内容帧才算首字节（`probe_frame.go`）②停滞看门狗按**字节**重置 + 渐进放宽 ③停滞时发**终止错误帧**而不是断连接（`writeTerminalError`）④熔断：连续失败阈值+冷却+失败探针翻倍 ⑤429 "saturated vs exhausted" 分类 ⑥每次尝试的 attempt trail |
| [thezillo/codex-proxy](https://github.com/thezillo/codex-proxy) | ✅ | Rust，8★ 但质量很高，活跃；**订阅账号代理**：OAuth 刷新、账号池、配额感知 | 我们**没有**的：token 刷新、账号池、配额路由 | ①到期前 300s 单飞刷新 + 401 强制刷新后**同账号重试一次** ②轮换后的 refresh_token **读-改-写**回写 auth.json（0600，不丢未知字段）③配额直接读响应的 `x-codex-*` 头（零额外请求）+ `/wham/usage` 只轮询过期账号 ④`usage_limit_reached` 与普通限流区分 ⑤账号级 4xx（401/403/429）vs 请求级 4xx 分类 |
| [ImBIOS/relay](https://github.com/ImBIOS/relay) | ⚠️ 描述偏乐观 | TypeScript/Bun，10★，3.5 个月未更新；**没有任何 OAuth 刷新**（Copilot token 30 分钟到期就失效），**代理里没有 429 处理**，轮换只在会话开始 | 参考价值最低；README 的"配额用尽自动轮换"实际是"下次开会话时才重新评估" | 双入口（Anthropic + OpenAI）单点协议分派；明确的客户端请求头白名单（去掉 UA/x-stainless，避免被上游识别成多用户） |
| [BerriAI/LiteLLM](https://github.com/BerriAI/litellm) | ✅ | MIT(main)/Rust+Python，~400MB 容器；**Responses 0% 迁移到 Rust**，是 bug 最多、迁移最少的路径（截至 2026-10 有开放的流式工具调用重放缺陷 #42955 等） | 大而全；**不建议整体替换**（我们的差异化不在它里面，且我们依赖的 Responses→chat 路径正是它最不稳的部分） | `allowed_fails` + `cooldown_time` + **按错误类别的策略**；重试纪律（限流/5xx 指数退避、通用错误立即重试、`retry_after` 下限）；会话/响应粘性；`x-litellm-response-cost` 让日志里的成本可被路由读取 |

## 本次已实现（借鉴 + 修 bug）

| 改动 | 借鉴自 | 说明 |
|---|---|---|
| **熔断器**（连续失败→开路→半开探针→失败翻倍，auth 单独长冷却，quota 到重置时间且**只延长不缩短**） | model-hotel、LiteLLM | 价格再便宜，刚 429/挂掉的中转也先歇着；状态写入 `bridge-state.json`，重启不丢 |
| **错误分类 + Retry-After** | model-hotel、codex-proxy、LiteLLM | `quota` / `auth` / `rate_limit` / `timeout` / `server` / `model_missing` / `bad_request`；识别 `usage_limit_reached` 等 8 种配额措辞；`resets_at`/`reset_at`/`reset_after_seconds` 各种拼写都认；**请求级 400 不记熔断**（不冤枉健康中转） |
| **内容感知首字节** | model-hotel | 以前读 1 个字节就算"落定"，而 `response.created` 这种记账事件也算 —— 中转接了请求就不吐内容时，客户端会一直挂。现在**等到第一个内容帧**（文本/推理/工具参数/`output_item.added`）才提交，之前都能换下一家 |
| **流中断看门狗 + 终止错误帧** | model-hotel | 提交后无法再换家，所以停滞 `BRIDGE_STREAM_STALL`（默认 45s，50 个 chunk 后 ×3）就补一个规范的 `response.failed`（`code=stream_stalled`）再收尾，客户端拿到可处理的错误而不是半截流 |
| **修掉真 bug：`read(n)` → `read1(n)`** | 测试中发现 | `HTTPResponse.read(n)` 会**阻塞到攒满 n 字节**，所以之前流式转发是按 8KB 批量走的；`read1` 每个上游 chunk 立即转发，首字延迟和流式观感都变好 |
| **状态接口显示熔断** | model-hotel attempt trail | `/__bridge/status` 每个中转带 `breaker` / `breaker_open_s` / `consecutive_fails` / `last_error` |
| **请求日志带错误类别** | 同上 | `bridge-requests.jsonl` 的重试行带 `error_kind` + `breaker` 判定（charge/noop） |

## 明确不借鉴（对单用户是过度设计）

- **请求对冲（hedging）**：给付费中转双倍花钱，单用户顺序 + 快速首字节超时更划算。
- **Postgres / Prometheus / Grafana / 多租户 / 虚拟密钥 / 预算 / 管理台**：model-hotel 和 LiteLLM 的这层是为多实例、多用户服务的；我们一个本机进程 + JSONL + `/__bridge/status` 就够。
- **LLM 分类器路由**（auto/complexity/quality/adaptive）：为路由再调一次模型，延迟与成本都不划算。
- **fleet 抗雪崩逻辑**（span-models 派生判定、配额探针白名单、24h pin、抖动）：那是防多副本同时打同一个上游的。
- **`ImBIOS/relay` 的 `~/.codex/config.toml` 直接覆盖**：会毁掉现有配置，绝不抄。
- **LiteLLM 的 no-DB 模式**：官方文档说明预算/支出日志全部依赖数据库；无库跑拿不到那部分价值。

## 下一步（按价值排序）

1. **订阅账号 OAuth 刷新 + 账号池**（照 `thezillo/codex-proxy` 移植）：到期前单飞刷新、401 强制刷新后同账号重试一次、轮换 token 读-改-写回 `auth.json`（0600）、多 `data_dir` 自动发现账号。我们现在只是"每次重读 auth.json"，token 一过期就只能干等。
2. **配额感知路由**：兜底账号的 `x-codex-primary/secondary-used-percent`、`reset-at` 直接进排序；`usage_limit_reached` 时把该账号 park 到重置时间（熔断的 quota 分支已经准备好了）。
3. **协议转换 = 组合而非重写**：Chat-Completions-only 的渠道（Kimi/Qwen/GLM 等）用 `codex-relay` 起一个 sidecar（一个 provider 一个端口），在我们这里注册成普通路由。**注意其 `previous_response_id` 会话存储在实例内**：跨实例故障转移会静默丢上下文，所以要么只在轮次边界切换，要么接受损失。
4. **会话粘性**：把同一会话（session-id / `prompt_cache_key`）固定到同一个中转，提高 prompt cache 命中（LiteLLM 的 session affinity 思路）。
5. **标签约束**：客户端用请求头表达 `cheap` / `!expensive`，排序在子集内进行（LiteLLM 的 tag routing，带否定）。

## 参考

- codex-relay：`src/quirks.rs`（quirk 注册表）、`src/stream.rs`（SSE 映射）、`src/session.rs`（会话存储与指纹）
- model-hotel：`internal/proxy/probe_frame.go`（内容帧定义）、`stream_reader.go`（看门狗）、`stream_finalize.go`（终止帧与熔断记账）、`internal/failover/circuitbreaker.go`
- codex-proxy：`src/auth/manager.rs`（刷新）、`src/auth/store.rs`（读-改-写）、配额与冷却相关配置在 `config.toml`
- LiteLLM：`docs/proxy/reliability`（allowed_fails/cooldown）、`docs/proxy/client_setup/codex_cli`（Codex 接入）、issues #42955/#27144（Responses 流式工具调用缺陷）
