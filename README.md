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

### 安装

```bash
git clone <本仓库> && cd codex-relay-toolkit
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
不修的话副本会指向别人的路由。

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

## 二、会话看门狗（`watchdog.py`）

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
