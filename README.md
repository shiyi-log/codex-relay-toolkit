# codex-relay-toolkit

两个小工具，用来让 **Codex** 跑在**第三方中转 / 代理供应商**上（就是那种把
`base_url` 指过去、OpenAI 兼容的中转），也可以配合
[CC Switch](https://github.com/farion1231/cc-switch) 的本地代理使用。

做这两个工具，是因为有两个可复现的缺口：

| 缺口 | 后果 |
|---|---|
| Codex 只重试它能分类的错误。中转发回 `HTTP 400 {"error":{"type":"upstream_error"}}`（很多中转在自己上游抖动时就是这么回的）会被归到 `codex_error_info: "other"`，**不会重试** —— 即使把 `request_max_retries` 设成 20 也没用 | 一个回合直接失败，而不是被重试 |
| CC Switch 的代理会在连接错误、404、5xx 上做故障转移，但把 `400` 当成**客户端错误**原样透传；而且它**每个请求对每个供应商只试 1 次**（实测：即使 `max_retries = 40`，日志也是 `已尝试 10/10 个 Provider`）| 中转的错误原样冒到最上层；调大 `max_retries` 完全无效 |

`bridge.py` 补上这两个缺口。`watchdog.py` 与网络无关：它负责把**异常结束、
且 Codex 自带 goal 引擎没有接管**的会话续跑起来。

> 与 `codex-relay` / `model-hotel` / `codex-proxy` / `LiteLLM` 等项目的逐项对标、
> 借鉴了什么、明确不抄什么、下一步做什么：见 [docs/ROADMAP.md](docs/ROADMAP.md)。

```
Codex ──► CC Switch 代理 ──► 重试桥 ──► 中转 A
   （或 ────────────────────► 重试桥）   中转 B
                                        …          ← 轮询，最多 N 次
                                        自己的 ChatGPT 账号（最后兜底）
```

---

## 一、重试桥（`bridge.py`）

一个零依赖的 HTTP 代理，**自己拥有重试循环**。

* **轮询所有已配置的中转**，每个请求最多 `BRIDGE_ATTEMPTS` 次（默认 100），
  从 CC Switch 选中的那个供应商开始。
* **重试这些**：`upstream_error` 信封、401/403/404/405、408/425/429、5xx，
  以及连接 / TLS / 读取失败。
* **其余原样透传** —— 真正的客户端错误不会被放大成重试风暴。
* **每次尝试都用该中转自己的 key 重新签名**，所以不同 key 的中转可以混着放。
* **拿到第一个字节才算"落定"。** 中转接受了请求却卡住或直接断开时，
  换下一家重试，而不是把一条截断的流丢给客户端。超时顺序保证桥**早于**上层发现：
  首字节 `50s` < CC Switch 的 `streaming_first_byte_timeout`。
* **按错误类型分别退避。** 连接 / TLS 类失败是成簇出现的（VPN 或隧道重载），
  而且**所有中转会同时失败**，所以给它们指数退避（上限 `BRIDGE_NET_BACKOFF_MAX`），
  让请求有机会熬过这段抖动；HTTP 类错误仍然快速轮询。
* **自己的 ChatGPT 账号作为最后兜底**（`auth_type: "oauth"`），
  **每次尝试都重新读** `~/.codex/auth.json`，不做缓存 ——
  缓存被轮换过的 token，正是"发出一个已吊销 token"的经典原因。
* **为官方后端归一化请求体。** 官方 Codex 后端要求 `input` 是数组
  （否则返回 `{"detail":"Input must be a list"}`），而中转容忍字符串。
  没有这一步，账号兜底会**静默失效**。
* **按中转能力自动适配模型。** 中转上新模型的节奏不一致，客户端要的模型某家没有时，
  桥会查该中转的 `/v1/models`，自动换成**它支持的、同家族里版本最高**的那个
  （例如客户端要 `gpt-6.1-sol`、某中转只有 `gpt-6-sol`，就自动降一级用它的），
  而不是直接吃一个 404 白耗一次轮询。模型列表按 `BRIDGE_MODELS_TTL` 缓存（默认 30 分钟）。
  如果该中转整个家族都没有，则不改写、按原样发出去。
* **顺序不是固定的：按「价格 + 实测速度」动态排。** 见下一节。

### 动态排序：又便宜、又快

`routes.json` 里的 `order` 只是**初始顺序**。运行期每个中转都会被打分，分数只来自能观测的东西：

| 维度 | 来源 |
|---|---|
| 价格 | 中转自己 `/v1/usage` 报的 `actual_cost`，折算成**加权百万 token 单价**，后台每 `BRIDGE_PRICE_TTL`（默认 600s）刷新 |
| 速度 | 本机实测的**首字节时间**（每次请求更新 EWMA，**同时按「一天中的哪个小时」分开记**）|
| 稳定性 | 衰减的失败率：卡住不出字节、超时、5xx |
| 模型匹配 | 要的模型这家有没有（只用缓存的 `/v1/models`，打分绝不发网络请求）|

**价格和速度都是变量**，所以这里没有一次性标定，全部是「最近观测」：

* **价格取最近 `BRIDGE_PRICE_DAYS`（默认 3 天）的账单**，不是历史平均值。
  `/v1/usage` 的 `daily_usage` 给出每天的真实扣费，用它算出「最近价格 / 历史价格」的趋势系数，
  再乘到每个模型的单价上。**昨天涨价的中转，今天不会继续显得便宜**（趋势上限 5 倍、下限 0.2 倍）。
* **超过 `BRIDGE_PRICE_MAX_AGE`（默认 6h）没更新过的价格直接不采信**，
  当作「没有数据」处理，而不是拿旧价格继续排。
* **速度按小时分桶**：`byhour` 里每个小时各自一条 EWMA，
  当前小时样本够 `BRIDGE_HOUR_MIN_SAMPLES`（默认 3）就优先用它，
  所以**高峰期慢下来只影响那个小时**，不会把这个中转一整天都压到底。
* **超过 `BRIDGE_LATENCY_MAX_AGE`（默认 30 分钟）没测过的速度同样不采信**。
* **每 `BRIDGE_EXPLORE_EVERY`（默认 50）个请求做一次探索**：把数据最陈旧的中转提到第一位重测一次。
  判定陈旧看的是「最后一次**尝试**」（成功或失败都算），所以一个永远失败的中转不会被当成
  「一直没测过」而每次都被翻上来；探索还带价格闸门
  （`BRIDGE_EXPLORE_PRICE_FACTOR`，默认只看最便宜的 2 倍以内）——
  价格变化本来就被后台 `/v1/usage` 刷新盯着，花请求去测一个贵 10 倍的中转快不快没有意义。
  这样「变快了/降价了」（或变慢/涨价）的中转能自己爬回来，而不是被一次坏运气永久埋掉；
  其余 49 个请求仍然老老实实走最便宜最快的那个。
* **从没测过的中转不会永远没有机会**：只要它的价格在「最便宜 × `BRIDGE_WARMUP_PRICE_FACTOR`
  （默认 2 倍）」以内，就先测它一次（每个这样的中转只多花一个请求）。
  否则「更快」永远无从谈起 —— 没人用过它，就永远不知道它快不快；
  而比最便宜的贵一倍以上的，不花这个钱去测。

规则：

* 分数越低越先试；**最便宜 + 最快 + 不需要降级模型**的排最前。
* 需要把模型降一级 → `+BRIDGE_MODEL_ADAPT_PENALTY`（默认 0.4）；整个家族都没有 → `+1.5`。
  **"便宜"永远不会悄悄变成"模型更差"。**
* 完全没有数据的中转按**中位数**参与排序（探索），不会因为没数据被打入冷宫，
  也不会凭空白嫖第一位。
* **会话粘性**：一轮对话里 Codex 每回合都会重发整段历史，桥按
  `session-id`/`thread-id` 头 → `prompt_cache_key` → `instructions`+首个输入项的哈希
  认出"同一个会话"，给**上一回合服务它的那家中转**加一个固定加成
  （`BRIDGE_AFFINITY_BONUS`，默认 0.75）。加成本身不会锁死：明显更便宜/更快的中转照样能赢；
  粘住的中转被熔断或消失时立刻放弃粘性。这样能保住上游的 **prompt cache**
  （以及有状态中转的会话上下文），也**不会**被预热/探索打断（有粘性时这两个让路）。
* **余额感知**：`/v1/usage` 刷新时顺带读回余额；低于 `BRIDGE_MIN_BALANCE`（默认 $1）
  就 park 这个中转（`BRIDGE_BALANCE_HOLD`，默认 1 小时），充值后自动解除 ——
  不用等打到没钱才失败。中转发出的天文数字（预付/无限套餐）按"无限制"处理。
* 排序用上一次的顺序做稳定排序的种子，分数接近时不会来回抖动。
* 官方订阅账号（`auth_type: "oauth"`）**永远排最后**，仍然单独限次。
* `BRIDGE_ORDER_MODE=fixed` 可以退回原来的行为：从 CC Switch 选中的那家开始轮询。
  `BRIDGE_RESPECT_START=1` 则是「仍然从中转选中那家开始，其余按分数排」。
* **冷启动**：第一次没有价格数据时，第一个请求最多等 `BRIDGE_PRICE_WAIT`（默认 8s）
  拿到第一轮价格再排（`bridge-state.json` 里还有新鲜数据时不需要等）。

价格是**绝对价格**，不是折扣率：`actual_cost` 除以加权 token 量
（output 记 4 倍、cache_read 记 0.1 倍、cache_creation 记 1.25 倍），
这样缓存命中率不同的中转也能公平比较（否则缓存多的那家看起来永远更便宜）。

看当前排序：

```bash
curl -s "http://127.0.0.1:15888/__bridge/status?text=1"
```

```
中转                       score    $/加权M 价格趋势     首字节  样本 失败率  模型  备注
哈吉米 特惠                  1.21      0.144   x1.00     320ms    18   0.00    ok
wdlink 福利                  1.83      0.214   x0.85     480ms   240   0.00    ok
pp 特惠                     2.05      0.051   x1.40     690ms   310   0.00    ok
pp pro                     3.10      0.230   x1.00       ?ms     0   0.00    ok  价格 4m前
wdlink  deepseek4.1        ──  查询不到用量（key 已失效）
```

* `价格趋势`：最近几天 ÷ 历史（`x1.40` = 这家最近涨价了，排序会相应后退）。
* 备注列还会出现 `余额 $x`、`会话粘住×N`（当前有 N 个会话钉在这家）、`熔断 120s`。
* `样本`／`首字节` 是实测值，超过 `BRIDGE_LATENCY_MAX_AGE` 没再测到就显示 `-`（不采信）。

* 去掉 `?text=1` 就是 JSON；加 `&model=gpt-6.1-sol` 看指定模型的排序。
* 学到的延迟／失败率／价格存在 `bridge-state.json`（已 git 忽略），重启不用从零开始。
* 只在**本机**可访问（非回环地址直接 403）。
* 每次排序变化会在 `bridge.log` 里留一行 `ORDER ...`，方便回看它为什么这么选。

### 看清楚「这一次到底是谁服务的」

CC Switch 的请求日志只会记 **它把请求发给了谁**——也就是它选中那家 = 桥的入口。
桥在内部换成别家，它完全不知道（这是"动态排序"的必然结果，不是 bug）。
所以要看清真相得看桥自己的逐请求日志：

```bash
tail -f ~/.ccswitch-retry-bridge/bridge-requests.jsonl
{"ts":"2026-10-07 04:49:41","mount":"pp 特惠","relay":"pp  plus","model":"gpt-6.1-sol",
 "attempt":1,"status":200,"result":"ok","first_byte_ms":4435,
 "tokens":{"input":1366,"output":96,"cache_read":168704,"cache_creation":0,"total":170166},
 "price_per_m":0.03074,"est_cost_usd":0.000572}
```

`mount` = CC Switch 发给了谁，`relay` = 桥实际用了谁，`attempt` = 第几次尝试，
`result` = `ok`/`pass`/`retry`/`stalled`/`network-error`。

**`tokens` 来自中转自己返回的 `usage`**（`input` 是**未缓存**输入，即 `input_tokens`
减去 `input_tokens_details.cached_tokens`）；**`est_cost_usd` = 该请求的加权 token ×
`price_per_m`**，而 `price_per_m` 是**这家自己的实测单价**（见上文，最近几天账单×趋势）。
所以这个金额是"这次请求在这家中转上实际该花多少"，比 CC Switch 的混合单价更接近真相。
流式响应里 usage 只在最后一个事件出现，桥用有界扫描（`UsageScanner`）解析，
不缓存整个响应；截断的 usage 一律丢弃，不会记半截数字。

文件到 5MB 自动轮转成 `.1`；`BRIDGE_REQUEST_LOG=0` 可关闭。`bridge.log` 的每行现在也带 `model=`。

**「主动动态调整」还是「失败重连」？以及花了多少钱：**

```bash
python3 request_stats.py                 # 默认读 ~/.ccswitch-retry-bridge
python3 request_stats.py --minutes 30
```

```
① attempt=1 且 relay == mount  :  1448  ( 73%)  入口那家就是第一名，没换
② attempt=1 且 relay != mount  :   504  ( 25%)  ★主动切换：前面没有任何失败
③ attempt >= 2                 :    42  (  2%)  失败重连（真正的 failover）

中转                          服务      主动      重连     首字节中位    加权M tok    估算花费$
pp  plus                   415     391      24     4435ms      0.248      0.01273
wdlink plus                 90      74      16     6070ms      0.000      0.00000

用量与花费（有 usage 的 17/1994 个请求）
  未缓存输入 19752   输出 4246   缓存读取 2111232   缓存命中率 99%
  加权百万 token: 0.248   估算总花费: $0.01273
```

**②才是"动态排序起作用"的证据**：请求在**第一次尝试**就发给了别家，不可能由失败触发。
③才是失败重连。

### 让 CC Switch 的请求日志显示真实中转（`relay_attrib.py`）

CC Switch 的「请求日志 / Provider 统计 / 模型统计」记的是**它把请求发给了谁**（桥的入口），
所以永远只有一家。但它那行的主键里带着中转返回的 response id：

```
request_id = session:codex:<它发去的 provider>:resp_xxxxxxxx
```

桥的 `bridge-requests.jsonl` 里既有 `response_id` 又有真正服务那家的 `relay_id`，
于是可以**精确到行**把归属改成真实值：

```bash
python3 relay_attrib.py --once            # 处理新请求
python3 relay_attrib.py --once --dry-run  # 只看会改什么
python3 relay_attrib.py --status          # 进度 / 已改多少行
python3 relay_attrib.py --rollback        # 按变更记录一键还原
```

后台常驻（每 30 秒一次）见 `launchd/com.local.relay-attrib.plist.example`。

规则与边界：

* **只改归属数据**（`proxy_request_logs.provider_id`），不动路由、不动金额、
  不碰 `data_source='codex_session'` 的会话同步行。
* 匹配靠 response id，**不会张冠李戴**；CC Switch 落库比响应晚一点，
  一时找不到的行会进重试队列（默认 15 分钟）而不是丢掉。
* 每次修改都追加到 `attribution-changelog.jsonl`，`--rollback` 按它还原。
* 只修正**桥重启之后**的请求（更早的行没记 response id，保持原样）。
* 幂等：重复跑不会重复改。

### 熔断与流式健康

价格和延迟是"平时谁更好"，但**刚出过事的中转必须先歇着**。这一层借鉴自
[model-hotel](https://github.com/hugalafutro/model-hotel)、
[codex-proxy](https://github.com/thezillo/codex-proxy) 和
[LiteLLM](https://github.com/BerriAI/litellm)：

* **熔断器**：连续 `BRIDGE_BREAKER_THRESHOLD`（默认 5）次失败 → 开路并冷却
  `BRIDGE_BREAKER_COOLDOWN`（60s）；冷却结束后的第一次真实请求就是**半开探针**，
  探针再失败则冷却翻倍（上限 `BRIDGE_BREAKER_COOLDOWN_MAX`，15 分钟）。
  开路的中转在排序里被压到队尾（不是移除，全部开路时仍会试）。
* **按错误类别区别对待**：`auth`（key 失效/无权限）直接 park 1 小时；
  `quota`（`usage_limit_reached`、余额不足…）park 到它给的**重置时间**（60s ~ 8 天），
  且**只延长不缩短**；普通 `rate_limit` 尊重 `Retry-After`（上限 60s）但不急着开路；
  `server`/`timeout`/`transport` 按连续次数累计。**请求级 400 与 404 不记熔断**，
  免得冤枉健康的中转。
* **等到内容才算落定**：以前读到一个字节就提交，而 `response.created` 这类记账事件也算；
  中转接了请求却不吐内容时客户端就干等。现在**等到第一个内容帧**
  （输出文本/推理/工具参数/`output_item.added`）才提交 —— 在那之前都还能换下一家。
* **流中断看门狗**：提交之后已经无法换家，所以一旦 `BRIDGE_STREAM_STALL`（默认 45s，
  收到 50 个 chunk 后放宽 ×3）没有任何字节，桥会补一个规范的
  `event: response.failed`（`code: stream_stalled`）再收尾 —— 客户端拿到可处理的错误，
  而不是一条被悄悄截断的流。
* 每次尝试的错误类别与熔断判定都写进请求日志（`error_kind` / `breaker`），
  `/__bridge/status` 也会显示每个中转的熔断状态与原因。

> 顺带修掉一个真 bug：桥原来用 `HTTPResponse.read(n)` 转发流，它会**阻塞到攒满 n 字节**，
> 等于把 SSE 按 8KB 批量转发；改成 `read1(n)` 后每个上游 chunk 立即转发。

### 订阅账号：OAuth 刷新与账号池

兜底路由（`auth_type: "oauth"`）用你自己的 ChatGPT 登录态。以前只是**每次尝试重读**
`~/.codex/auth.json` —— token 一过期就只能干等。现在补齐了
[codex-proxy](https://github.com/thezillo/codex-proxy) 那套，并加了一道它没有的跨进程保护：

* **刷新时机**（`BRIDGE_OAUTH_REFRESH`）：
  * `reactive`（默认）：**只在 token 真的过期**、或账号后端回 `401` 时才刷新；
  * `on`：到期前 `BRIDGE_OAUTH_SKEW`（默认 300s）就刷新 —— 适合桥独占这个 `CODEX_HOME`；
  * `off`：从不刷新（回到旧行为）。
* **401 → 强制刷新 → 同账号重试一次**，这次重试不占尝试预算，客户端看不到失败。
* **轮换后的 token 读-改-写回文件**：只动 `tokens` 与 `last_refresh`，
  `OPENAI_API_KEY` 和我们不认识的字段原样保留；`tmp + fsync + rename` 原子写，权限 0600。
* **不跟 Codex app 抢**：刷新前先拿 `<auth.json>.bridge-lock` 的跨进程 `flock`，再**重读文件**；
  如果 app 已经换过 token，就直接用它的、不再二次轮换 —— 两个刷新者会把彼此的 refresh token 作废
  （codex-proxy 的 README 专门警告过这一点）。
* **账号池**：`BRIDGE_OAUTH_DIRS`（默认 `~/.codex`）下的 `auth.json` —— 目录本身 **加一层子目录** ——
  每个都成为一条独立兜底路由，`setup.py` 自动发现并生成；某个账号额度用完会被 park，其他账号顶上。
* **配额直接读响应头**（零额外请求）：`x-codex-{primary,secondary}-used-percent`、
  `-window-minutes`、`-reset-at` / `-reset-after-seconds`，外加 `x-codex-plan-type` 与
  `x-codex-credits-balance`。任一窗口 ≥ 100% 就把该账号 park 到重置时间（**只延长不缩短**），
  后续响应若又报告 < 100% 则自动解除。`/__bridge/status` 会显示已用百分比、套餐、重置倒计时、token 余期。

> **共存提示**：桥和 Codex app 共用同一个 `auth.json`。默认的 `reactive` 只在 token 已经死了才动手，
> 风险最低；若希望桥主动刷新，建议给桥一个独立的 `CODEX_HOME`
> （`BRIDGE_OAUTH_DIRS=/path/to/bridge-codex`，在那里单独 `codex login` 一次）。

### 安装

```bash
git clone <本仓库> && cd codex-relay-toolkit
python3 setup.py --dry-run         # 先看会改什么，不动数据库/路由文件
python3 setup.py                 # 读取 CC Switch 数据库、生成 routes.json，
                                 # 并把每个供应商的 base_url 指到桥上
# 可以先检查一下 routes.json，然后启动：
python3 bridge.py                # 前台运行；后台服务见 launchd/
```

`setup.py` 会把它投影出来的客户端配置做归一化，让 app **仍然显示官方 ChatGPT 登录**，
但流量实际走中转（`name = "OpenAI"`、`requires_openai_auth = true`、
`supports_websockets = false`、去掉 `http_headers`，
并剥掉已废弃的 `[features.guardianv2].thread_context`）。

在 CC Switch 里**新增或复制**供应商之后，重新跑一次 `setup.py`。它是幂等的，
并且会修复"复制陷阱"：**在界面里复制出来的供应商，会继承被复制者的桥地址**，
不修的话副本会指向别人的路由 —— 而且**只从桥地址是看不出它真正是谁的**。
所以副本的真实地址按这个顺序确定：

1. 该供应商自己记录过的原始 `base_url`（`originals.json`／上一轮的 route）；
2. CC Switch 里这家供应商的 `website_url`（新增中转时填的官网地址）；
3. 上面两个都没有 → **跳过并提示**，不会拿"被复制者的上游"顶替。

候选地址还要用**这家供应商自己的 key** 去探一次 `/models`（`""` 和 `/v1` 都试），
只看它是否真的应答；探不通会打印 `WARN` 但仍然入队（有些中转只是禁了 `/models`）。
这套判断有回归测试：

```bash
python3 tests/test_setup_discovery.py    # 本地假中转 + 临时数据库，不碰网络
```

```bash
python3 restore.py               # 把原始 base_url 还原回去
```

### 配置

全部通过环境变量（见 `launchd/*.plist.example`）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BRIDGE_PORT` | `15888` | 监听端口 |
| `BRIDGE_ATTEMPTS` | `100` | 每个请求最多尝试次数 |
| `BRIDGE_MAX_SECONDS` | `600` | 单请求总时长上限（`0` = 不限）|
| `BRIDGE_BACKOFF` | `0.15` | HTTP 类重试的基础间隔 |
| `BRIDGE_NET_BACKOFF_MAX` | `8` | 连接类失败退避的上限 |
| `BRIDGE_NET_FAIL_LIMIT` | `15` | **连续**这么多次连接失败就中止请求（整网断掉时应该快速失败，而不是干等）|
| `BRIDGE_FIRST_BYTE_TIMEOUT` | `50` | 等首字节多久后换下一家 |
| `BRIDGE_TIMEOUT` | `600` | 流已开始后的读取超时 |
| `BRIDGE_BACKLOG` | `256` | listen backlog（标准库默认只有 5，会丢连接）|
| `BRIDGE_EXHAUST_STATUS` | `400` | 预算用尽时返回的状态码 |
| `BRIDGE_OFFICIAL_ATTEMPTS` | `10` | 兜底账号的单请求次数上限 |
| `BRIDGE_MODELS_TTL` | `1800` | 中转模型列表缓存秒数（自动适配模型用）|
| `BRIDGE_ORDER_MODE` | `adaptive` | `adaptive`＝按价格+实测速度动态排；`fixed`＝原来的固定轮询 |
| `BRIDGE_RESPECT_START` | `0` | `1`＝仍然先试 CC Switch 选中的那家，其余按分数排 |
| `BRIDGE_W_PRICE` | `1.0` | 排序公式里价格的权重 |
| `BRIDGE_W_LATENCY` | `1.0` | 排序公式里首字节时间的权重 |
| `BRIDGE_W_FAIL` | `2.0` | 排序公式里失败率的权重 |
| `BRIDGE_MODEL_ADAPT_PENALTY` | `0.4` | 需要降级模型时加的罚分 |
| `BRIDGE_MODEL_MISSING_PENALTY` | `1.5` | 整个模型家族都没有时加的罚分 |
| `BRIDGE_PRICE_TTL` | `600` | 价格（`/v1/usage`）刷新间隔秒数 |
| `BRIDGE_PRICE_TIMEOUT` | `8` | 查询价格的单次超时 |
| `BRIDGE_PRICE_WAIT` | `8` | 冷启动时首个请求最多等多久拿到第一轮价格（有 `bridge-state.json` 时不需要等）|
| `BRIDGE_PRICE_DAYS` | `3` | 价格取最近几天的账单（趋势系数）|
| `BRIDGE_PRICE_MAX_AGE` | `21600` | 超过这么久没刷到的价格不采信（秒）|
| `BRIDGE_LATENCY_MAX_AGE` | `1800` | 超过这么久没测过的速度不采信（秒）|
| `BRIDGE_HOUR_MIN_SAMPLES` | `3` | 「当前小时」的延迟样本达到这么多才优先用它 |
| `BRIDGE_EXPLORE_EVERY` | `50` | 每多少个请求重测一次最陈旧的中转（`0` = 关闭探索）|
| `BRIDGE_EXPLORE_PRICE_FACTOR` | `2.0` | 探索只挑「最便宜的几倍」以内的中转（`0` = 不限价格）|
| `BRIDGE_WARMUP_PRICE_FACTOR` | `2.0` | 从没测过的中转，价格在「最便宜的几倍」以内就先测一次（`0` = 关闭）|
| `BRIDGE_EWMA_ALPHA` | `0.3` | 延迟/失败率的新样本权重（越大跟得越快、越抖）|
| `BRIDGE_MIN_BALANCE` | `1.0` | 余额低于这个数（美元）就 park 该中转 |
| `BRIDGE_BALANCE_HOLD` | `3600` | 余额不足时的 park 时长 |
| `BRIDGE_UNLIMITED_BALANCE` | `1000000` | 大于此值视为"无限制"，不参与余额判断 |
| `BRIDGE_AFFINITY_TTL` | `1800` | 会话粘性的存活时间（秒，按最后一次使用算）|
| `BRIDGE_AFFINITY_BONUS` | `0.75` | 粘性中转在排序里的加成（分数越低越好）|
| `BRIDGE_AFFINITY_MAX` | `2000` | 同时记住多少个会话 |
| `BRIDGE_OAUTH_REFRESH` | `reactive` | `reactive`＝token 死了/401 才刷新；`on`＝到期前也刷新；`off`＝从不 |
| `BRIDGE_OAUTH_DIRS` | `~/.codex` | 账号池搜索目录（`:` 分隔；目录本身 + 一层子目录里的 `auth.json`）|
| `BRIDGE_OAUTH_SKEW` | `300` | `on` 模式下提前多少秒刷新 |
| `BRIDGE_OAUTH_ISSUER` | `https://auth.openai.com` | 刷新端点 |
| `BRIDGE_OAUTH_CLIENT_ID` | Codex CLI 的公开 client id | 刷新用的 OAuth client |
| `BRIDGE_OAUTH_TIMEOUT` | `30` | 刷新请求超时 |
| `BRIDGE_BREAKER_THRESHOLD` | `5` | 连续失败多少次开路 |
| `BRIDGE_BREAKER_COOLDOWN` | `60` | 基础冷却秒数（失败探针翻倍）|
| `BRIDGE_BREAKER_COOLDOWN_MAX` | `900` | 冷却上限 |
| `BRIDGE_BREAKER_AUTH_COOLDOWN` | `3600` | key 失效时的 park 时长 |
| `BRIDGE_BREAKER_QUOTA_MIN` / `_MAX` | `600` / `691200` | 配额 park 的钳制区间（10 分钟 ~ 8 天）|
| `BRIDGE_RETRY_AFTER_MAX` | `60` | 尊重 `Retry-After` 的上限 |
| `BRIDGE_TTFT_BUFFER_MAX` | `524288` | 等首个内容帧时最多缓冲的字节 |
| `BRIDGE_STREAM_STALL` | `45` | 流中无字节多久算停滞 |
| `BRIDGE_STREAM_STALL_MULT` / `_AFTER` | `3` / `50` | 收到这么多 chunk 后停滞阈值放宽的倍数 |
| `BRIDGE_STATE` | `<脚本目录>/bridge-state.json` | 学到的排序数据（**不要**用 watchdog 的 `state.json`）|
| `BRIDGE_REQUEST_LOG` | `<脚本目录>/bridge-requests.jsonl` | 逐请求日志（mount/relay/model/attempt/首字节）；`0` = 关闭 |
| `BRIDGE_HOUSEKEEPING` | `30` | 后台线程轮询间隔（刷新价格、落盘状态）|
| `BRIDGE_PROBE_TIMEOUT` | `10` | `setup.py` 校验候选上游时等的秒数 |
| `BRIDGE_DB` | `~/.cc-switch/cc-switch.db` | 换一个数据库（测试用）|
| `BRIDGE_ROUTES` / `BRIDGE_ORIGINALS` | 脚本同目录 | 换 `routes.json` / `originals.json` 的路径（测试用）|

> **一定要调文件描述符上限。** launchd 的默认软上限是 256，一个流式代理很快就会用尽 ——
> 症状是**上层报 `connection failed`，而桥看起来一切正常**。
> 示例 plist 里设了 `SoftResourceLimits → NumberOfFiles = 8192`。

### `routes.json` 字段说明

`setup.py` 自动生成这个文件；手写时可参考 `routes.example.json`（**合法 JSON，不含注释**）。

| 字段 | 含义 |
|---|---|
| `port` | 监听端口 |
| `attempts` | 每个请求最多轮询次数 |
| `order` | 轮询顺序（按 CC Switch 的故障转移优先级，兜底账号排最后）|
| `routes.<id>.mount` | 桥暴露的路径前缀；CC Switch 的 `base_url` 指向 `http://127.0.0.1:<port><mount><prefix>` |
| `routes.<id>.prefix` | 该中转真实 `base_url` 的路径后缀（`""` 或 `/v1` 等）|
| `routes.<id>.upstream` | 中转的 `scheme://host` |
| `routes.<id>.auth` | 中转的 API key —— **属于机密** |
| `routes.<id>.auth_type` | `"bearer"`（默认）或 `"oauth"`（读 Codex 的 `auth.json`，即 ChatGPT 登录态）|
| `routes.<id>.auth_file` | `auth_type` 为 `oauth` 时的凭据文件路径 |
| `routes.<id>.max_attempts` | 该路由的可选次数上限（兜底账号用它限流）|

### 超时顺序很关键

顺序搞错的话，永远是外层先掐断请求，桥根本没机会换路：

```
桥的首字节 (50s)   <   CC Switch streaming_first_byte_timeout (180s)
桥的读取/静默 (600s) ≤  CC Switch streaming_idle_timeout       (600s)
```

---

## 二、自动跟随最新模型（`sync_model.py`）

**不要把模型名写死。** 中转上新模型的速度不一样，写死一个版本意味着要么用旧的，
要么每次手动改。

`sync_model.py` 查询每个中转的 `/v1/models`，挑出**同一家族里版本最高、
且被足够多中转支持**的那个模型，然后同时写进 `~/.codex/config.toml` 和
CC Switch 里各供应商的配置。

```bash
python3 sync_model.py            # 只显示会选哪个，不改任何东西
python3 sync_model.py --apply    # 真正写入（会先优雅停掉 CC Switch）
```

判断规则：

* 家族默认 `sol`（`BRIDGE_MODEL_FAMILY` 可改）；不指定时沿用当前模型的家族
* 版本按 `gpt-<大版本>[.<小版本>]-<家族>` 排序取最高
* 支持率要求 `>= BRIDGE_MODEL_MIN_SUPPORT`（默认 `0.5`）。
  **用「大多数」而不是「全部」**：个别中转会掉队（比如只有它没上 6.1），
  不该让整个池子陪它退回旧版本
* 一个模型都不支持的中转会被单独列出来 —— 它们只会白耗轮询次数

> 注意：查询 `/models` 时会带一个常见的 `User-Agent`。有些中转会对
> `Python-urllib/x.y` 直接回 403，导致模型列表被误判成"不支持"。

模型对不上的中转建议移出队列，否则每次轮询到它都是 404。

---

## 三、真实计费同步（`sync_usage.py`）

CC Switch 的成本统计靠它内置的 `model_pricing` 表算：查不到这个模型就记 0，
界面显示"未定价"（日志 `[USG-002] 模型定价未找到`）。而中转站其实**自己就报了真实费用** ——
大多数 one-api / new-api 系中转的 `GET /v1/usage` 里，`model_stats[]` 每个模型都带：

| 字段 | 含义 |
|---|---|
| `cost` | 按标价算 |
| `actual_cost` | **实际计费**（你被收的钱） |
| `account_cost` | 账户扣费 |

`sync_usage.py` 把这些数字汇总，按 token 量加权算出「中转实际收费 / 标价」的系数，
再把折算后的真实单价写回 `model_pricing`：

```bash
python3 sync_usage.py --balance                # 余额查询：各中转真实余额 + 地址绑定检查
python3 sync_usage.py                          # 只看各中转的折算系数
python3 sync_usage.py --write-pricing          # 看折算后的单价
python3 sync_usage.py --write-pricing --apply  # 写入（推荐，会先备份旧单价）
python3 sync_usage.py --field account_cost --write-pricing --apply   # 换字段
```

### 余额查询（`--balance`）

```
中转                       HTTP               余额 单位    planName     余额地址绑定
哈吉米 特惠                200        29.9997246 USD   钱包余额         ok
wdlink 福利                200       84.61191884 USD   钱包余额         ok
```

两件事一起查：

* **余额**：用每个中转**自己的 key** 查它**自己的**地址（`upstream` + 该中转的
  `prefix`，所以只有 `/v1` 的中转和只认站点根的中转都对）。
* **地址绑定**：对比 CC Switch 里这家供应商的 `usage_script.baseUrl`。
  复制出来的中转会把这个字段一起复制走 —— 于是界面上显示的是**别人家的余额**。
  这一列会直接标 `错: https://…（应 https://…）`，跑一次 `setup.py` 就能对齐。
  查不到余额的中转会把自己的 HTTP 状态和错误原因打出来，不再静默变成"没有数据"。

### 真实价格查询

每个中转的金额一律取它自己的 `/v1/usage`。第一次跑（对着 CC Switch 内置标价）时，
实测各中转的系数在 **0.03 ~ 0.42** 之间（普遍打大折扣），同样一次请求的成本
从 `$0.00958` 变成 `$0.00065`。

> 系数是「中转报告的收费 ÷ **当前表里的**单价」。第一次折算完之后表里已经是真实单价，
> 所以**再跑一次系数会趋近 `1.000`** —— 这是收敛，不是又打了折。之后每次跑只是把
> 新出现的中转／新用法带进来的偏差修正回 1。

> 踩过的坑：改 `providers.cost_multiplier`（`--apply` 的默认模式）**CC Switch 记账时并不采用**，
> 记录里仍是 `1.0`，重启也不生效 —— 所以推荐用 `--write-pricing` 直接改单价。
> 两种 `--apply` 都会先把旧值备份成 `model_pricing-bak-<时间>.json` /
> `cost_multiplier-bak-<时间>.json`（已 git 忽略）。

---

## 四、会话看门狗（`watchdog.py`）

Codex **本身就会**自动续跑带 `active` goal 的会话（goal 引擎的数据在
`~/.codex/goals_1.sqlite`）。在一台繁忙机器上实测 48 小时：
**24 次异常结束里，22 次已经被它自己续跑了** —— goal 为 `active` 的 6/6 全部续跑。

这个看门狗只补**剩下的那部分**：回合因错误中断、之后**没有**任何新任务开始、
且 goal 不存在或不是 `active`。它通过官方 CLI 续跑：

```bash
codex queue --thread <uuid> --message "…从中断处继续…"
```

异常结束很好判定 —— rollout 里会记录
`{"type":"task_complete","error":{…}}`。

因为这个东西会**自动花 token**，所以带了护栏：

* 只在错误之后**没有** `task_started` 时才动作（绝不和 Codex 自己的续跑抢）
* 只在 rollout 静默 ≥ `IDLE_SECONDS` 之后
* **绝不**碰 `paused` / `complete` / `blocked` / `usage_limited` / `budget_limited` 的 goal
* 每个会话有冷却，并且有滚动每小时上限
* 全局急停开关：`touch DISABLED`
* `--dry-run` 只打印"会做什么"，不做任何改动

```bash
python3 watchdog.py --dry-run
python3 watchdog.py                 # 跑一轮；后台服务见 launchd/
```

---

## 安全

* **`routes.json` 含每个中转的 API key**，写入时权限为 `600`。
  它已被 git 忽略，仓库里只有 `routes.example.json`。
  提交前务必确认：`git status --porcelain` 不能出现它。
* `originals.json`、`state.json` 和 `*.log` 同理被忽略
  （日志含中转名和错误响应体；`state.json` 含会话 id）。
* 桥**从不**记录请求体或 `Authorization` 头。

## 已知限制

* 流**已经开始输出之后**被截断，任何代理都无法透明重试 —— 客户端已经看到部分内容了。
  这里只能恢复"首字节之前"就被掐断的情况。
* **本机**网络 / 隧道整体断掉时，中转和上游账号会**同时**不可用，再怎么重试都没用。
* `setup.py` 依赖 CC Switch 的 SQLite 结构（`providers`、`proxy_config`）。
  那边改表结构的话，这里也要跟着改。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。
