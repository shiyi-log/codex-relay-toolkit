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
