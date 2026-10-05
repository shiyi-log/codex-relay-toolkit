# codex-relay-toolkit

Two small tools for running **Codex** against **third-party relay / proxy
providers** (the OpenAI-compatible kind you point `base_url` at), optionally
behind [CC Switch](https://github.com/farion1231/cc-switch)'s local proxy.

They exist because of two concrete, reproducible gaps:

| Gap | Consequence |
|---|---|
| Codex only retries errors it can classify. A relay that answers `HTTP 400 {"error":{"type":"upstream_error"}}` — which is what many relays return when *their* upstream hiccups — lands in `codex_error_info: "other"` and is **not** retried, even with `request_max_retries = 20`. | A turn dies instead of being retried. |
| CC Switch's proxy fails over on connection errors, 404 and 5xx, but treats `400` as a *client* error and passes it straight through. It also tries **each provider at most once per request** (measured: its log says `tried 10/10 providers` even with `max_retries = 40`). | The relay error surfaces unchanged; raising `max_retries` does nothing. |

`bridge.py` fills both gaps. `watchdog.py` is unrelated to networking: it
resumes a Codex session that ended abnormally and was *not* picked up by
Codex's own goal engine.

```
Codex ──► CC Switch proxy ──► bridge ──► relay A
   (or ──────────────────────► bridge)   relay B
                                          …        ← round-robin, N attempts
                                          own ChatGPT account (last resort)
```

---

## 1. Retry bridge (`bridge.py`)

A dependency-free HTTP proxy that owns the retry loop.

* **Round-robins every configured relay**, up to `BRIDGE_ATTEMPTS` (default 100)
  attempts per request, starting from whichever provider CC Switch selected.
* **Retries the retryable set**: the `upstream_error` envelope, 401/403/404/405,
  408/425/429, 5xx, and connection/TLS/read failures.
* **Passes everything else through untouched** — a genuine client error is not
  turned into a retry storm.
* **Re-signs each attempt with that relay's own key** (relays with different API
  keys can be mixed freely).
* **Commits on the first byte.** A relay that accepts the request and then stalls
  or closes is retried on the next relay instead of handing the client a
  truncated stream. Timeouts are ordered so the bridge notices *before* the
  layer above gives up: first byte `50s` < CC Switch's `streaming_first_byte_timeout`.
* **Backs off per failure kind.** Connection/TLS failures arrive in bursts (VPN
  or tunnel reloads) and hit *every* relay at once, so they get exponential
  backoff (up to `BRIDGE_NET_BACKOFF_MAX`) to ride the outage out; HTTP-level
  retries stay fast.
* **Own ChatGPT account as last resort** (`auth_type: "oauth"`), read fresh from
  `~/.codex/auth.json` on every attempt — no caching, because caching a rotated
  token is exactly how you end up sending a revoked one.
* **Normalises the request for the official backend.** The ChatGPT Codex backend
  rejects `input` as a bare string (`{"detail":"Input must be a list"}`) while
  relays accept it. Without this the account fallback silently never works.

### Install

```bash
git clone <this repo> && cd codex-relay-toolkit
python3 setup.py                 # reads CC Switch's DB, writes routes.json,
                                 # repoints every provider at the bridge
# review routes.json if you like, then start it:
python3 bridge.py                # foreground; see launchd/ for a service
```

`setup.py` normalises the client config it projects so the app keeps showing the
official ChatGPT login while traffic actually goes to the relays
(`name = "OpenAI"`, `requires_openai_auth = true`,
`supports_websockets = false`, no `http_headers`, and it strips the deprecated
`[features.guardianv2].thread_context`).

Re-run `setup.py` after adding / duplicating a provider in CC Switch. It is
idempotent, and it repairs the duplicate trap: **a provider cloned in the UI
inherits the bridge URL of the provider it was cloned from**, so the copy would
otherwise point at someone else's route.

```bash
python3 restore.py               # put the original base_urls back
```

### Configuration

All via environment variables (see `launchd/*.plist.example`):

| Variable | Default | Meaning |
|---|---|---|
| `BRIDGE_PORT` | `15888` | listen port |
| `BRIDGE_ATTEMPTS` | `100` | max attempts per request |
| `BRIDGE_MAX_SECONDS` | `600` | wall-clock cap per request (`0` = off) |
| `BRIDGE_BACKOFF` | `0.15` | base delay between HTTP-level retries |
| `BRIDGE_NET_BACKOFF_MAX` | `8` | cap for connection-failure backoff |
| `BRIDGE_NET_FAIL_LIMIT` | `15` | abort the request after this many consecutive connection failures (a total network outage should fail fast, not hang) |
| `BRIDGE_FIRST_BYTE_TIMEOUT` | `50` | wait for the first byte before rotating |
| `BRIDGE_TIMEOUT` | `600` | read timeout once a stream is running |
| `BRIDGE_BACKLOG` | `256` | listen backlog (the stdlib default of 5 drops connections) |
| `BRIDGE_EXHAUST_STATUS` | `400` | status returned when the budget is spent |
| `BRIDGE_OFFICIAL_ATTEMPTS` | `10` | per-request cap for the fallback account |

> **Set the file-descriptor limit.** launchd's default soft limit is 256, which a
> streaming proxy exhausts quickly — the symptom is the layer above reporting
> `connection failed` while the bridge looks perfectly healthy. The example plist
> sets `SoftResourceLimits → NumberOfFiles = 8192`.

### Timeout ordering matters

Get this wrong and the outer layer always kills the request first, so the bridge
never gets to rotate:

```
bridge first-byte (50s)  <  CC Switch streaming_first_byte_timeout (180s)
bridge read/idle (600s)  ≤  CC Switch streaming_idle_timeout     (600s)
```

---

## 2. Session watchdog (`watchdog.py`)

Codex **already** auto-continues threads that have an `active` goal (its goal
engine lives in `~/.codex/goals_1.sqlite`). Measured on one busy machine over
48h: **22 of 24 abnormal turn endings were already resumed by it** — for
`active` goals, 6/6.

This watchdog only covers the remainder: a turn that died with an error,
started nothing afterwards, and whose goal is absent or not `active`. It resumes
through the supported CLI:

```bash
codex queue --thread <uuid> --message "…continue from where it broke…"
```

Abnormal endings are easy to detect — the rollout records
`{"type":"task_complete","error":{…}}`.

Because this spends tokens automatically, it has rails:

* only when **no** `task_started` followed the error (never races Codex's own resume)
* only after the rollout has been idle ≥ `IDLE_SECONDS`
* **never** touches `paused` / `complete` / `blocked` / `usage_limited` / `budget_limited` goals
* per-thread cooldown and a rolling hourly cap
* global kill switch: `touch DISABLED`
* `--dry-run` prints what it *would* do and changes nothing

```bash
python3 watchdog.py --dry-run
python3 watchdog.py                 # one cycle; see launchd/ for a service
```

---

## Security

* **`routes.json` contains every relay's API key** and is written `chmod 600`.
  It is git-ignored; only `routes.example.json` ships. Check before you commit:
  `git status --porcelain` must not list it.
* `originals.json`, `state.json` and `*.log` are git-ignored for the same reason
  (logs contain relay names and error bodies; `state.json` contains thread ids).
* The bridge never logs request bodies or `Authorization` headers.

## Limitations

* A stream cut **after** it has started delivering cannot be transparently
  retried by any proxy — the client already saw partial output. Only truncation
  *before* the first byte is recoverable here.
* A **local** network/tunnel outage takes down the relays *and* the upstream
  account at the same time; no amount of retrying fixes that.
* `setup.py` reads CC Switch's SQLite schema (`providers`, `proxy_config`).
  A schema change there will need a matching change here.

## 中文速览

* `bridge.py`：本地重试桥。Codex 不重试中转发来的 `HTTP 400 upstream_error`，
  CC Switch 也把它当客户端错误直接透传、且**每个请求对每个供应商只试 1 次**，
  所以重试必须自己做。桥负责跨中转轮询、按各自 key 重新签名、按错误类型退避，
  并在最后用你自己的 ChatGPT 账号兜底。
* `setup.py` / `restore.py`：把 CC Switch 里的供应商指向桥 / 还原。
* `watchdog.py`：Codex 自带 goal 自动续跑（48 小时内 24 次异常里已自动续跑 22 次），
  这个脚本只补"真的停住了"的那部分，用官方 `codex queue` 续跑，带冷却和次数上限。
* 超时顺序很关键：**桥的首字节超时必须早于上层**，否则永远是上层先掐断。
* `routes.json` 含 API key，已被 `.gitignore` 排除，不要提交。

## License

MIT — see [LICENSE](LICENSE).
