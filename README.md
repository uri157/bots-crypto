# bots-crypto

Minimal **trading-bot** stack targeting **Binance USDⓈ-M Futures**. It comprises:

* **marketdata** — subscribes to Binance-style **WebSocket** klines (and optional mark price) and writes the **latest closed candle** to **Redis** (key + pub/sub).
* **ema** — EMA-cross strategy that **polls** Redis for the latest closed candle, sizes entries, places **MARKET** orders and **reduce-only STOP_MARKET** exits via a Binance Futures adapter, and (optionally) **logs trades** to a DB.
* All services expose **`/healthz`** and **`/metrics`** (Prometheus) and are configured via **environment variables**.

> The bot talks to **Binance UM Futures** (or any compatible gateway). If you point it at a local simulator, it should work transparently as long as it mirrors Binance’s **futures** endpoints.

> **Note:** This layout is the **baseline architecture for the main branch** going forward. Additional strategies will plug into the same interfaces without changing the core services.

---

## Topology

```
[Binance UM Futures (or compatible gateway)]
                 ↑                │
                 │  WS /stream    │  Redis
                 │                ▼
          [marketdata]  →  key:  candle:last:{SYMBOL}:{TF}
                          pubsub: candles:{SYMBOL}:{TF}

                 └────────────── HTTP /fapi/v1/*, /fapi/v2/* ───────────→  [ema]
```

* `marketdata` subscribes to `${symbol}@kline_${TF}`; on **closed candle** (`x=true`) it:

  * sets `candle:last:{SYMBOL}:{TF}` (raw kline JSON), and
  * **publishes** the same payload on `candles:{SYMBOL}:{TF}` (pub/sub).
* `ema` **polls** the `candle:last:{SYMBOL}:{TF}` key (no pub/sub consumption in this branch) and reacts when the stored candle changes.

---

## Repository layout

```
bots-crypto/
├─ common/
│  ├─ exchange.py        # Async adapter over Binance UM Futures (binance-connector)
│  └─ locks.py           # (not used by EMA runner in this branch)
├─ datafeed/
│  ├─ market_ws.py       # WS client for klines (and mark price)
│  └─ funding.py         # (stubs; not enforced by EMA)
├─ ema/
│  ├─ config.py          # Reads EMA env vars (fast/slow, alloc, stop, allow_shorts)
│  ├─ strategy.py        # EmaCrossStrategy → "LONG" | "SHORT" | "NONE"
│  ├─ executor.py        # Sends MARKET + reduce-only STOP_MARKET; optional DB logging
│  └─ services/          # orders.py, stops.py helpers
├─ runner/
│  ├─ marketdata.py      # WS → Redis (key + pub/sub), /healthz, /metrics
│  └─ ema.py             # Poll Redis key → strategy/executor, /healthz, /metrics
├─ storage/
│  ├─ db.py              # DuckDB helpers via DB_PATH (legacy module)
│  ├─ models.py          # tables: orders, fills, positions, ledger, metrics
│  └─ (no HTTP sink in this branch)
├─ metrics/server.py     # optional central registry (services also self-expose)
├─ docker-compose.yml
├─ Dockerfile
├─ schemas.sql
├─ requirements.txt
└─ .env.sample
```

---

## Docker Compose services

* `redis`
* `marketdata` (default **:9002**)
* `ema` (default **:9001**)

Each exposes **`/healthz`** and **`/metrics`** on its HTTP port.

---

## Environment variables

Copy defaults and edit:

```bash
cp .env.sample .env
```

> **Heads-up:** `.env.sample` still contains legacy keys (e.g. `EQUITY_USDT`,
> `RISK_PCT_PER_TRADE`) from the old sizing engine. Set the documented
> variables below (`ALLOC_USDT`, `STOP_PCT`, etc.) manually – the sample file
> doesn’t include them yet.

**Common**

* `REDIS_URL` — e.g. `redis://redis:6379/0`
* `LOG_LEVEL` — `INFO` or `DEBUG`
* `BINANCE_BASE_URL` — REST base, e.g. `http://host.docker.internal:9010` or `https://fapi.binance.com`
* `BINANCE_WS_BASE` — WS base, e.g. `ws://host.docker.internal:9010` or `wss://fstream.binance.com`
* `TESTNET` — `true|false` (passed to EMA config if needed)

**marketdata**

* `SYMBOLS` — e.g. `BTCUSDT` (comma-separated to track multiple)
* `TF_PRIMARY` — e.g. `1h` (and optional `TF_SECONDARY=15m`)
* `HTTP_PORT` — default `9002`

**ema (strategy/execution)**

* `SYMBOL` — single symbol, e.g. `BTCUSDT`
* `TF_PRIMARY` — timeframe to read from Redis (must match marketdata)
* `EMA_FAST`, `EMA_SLOW` — EMA windows (e.g. `2`, `4`)
* `ALLOC_USDT` — notional per entry sizing
* `STOP_PCT` — reduce-only stop distance (e.g. `0.01` for 1%)
* `EMA_ALLOW_SHORTS` — `true|false`
* `BOT_ID` — metrics label (default `ema`)
* `HTTP_PORT` — default `9001`

**Storage (optional trade logging)**

* `DB_ENABLED` — `true|false`
* `DB_URL` — DB connection string (Postgres / DuckDB / SQLite). See `schemas.sql`.

> Linux tip: if `host.docker.internal` doesn’t resolve, use your host IP or run your gateway/simulator in the same compose and target its service name.

---

## Quick start (Docker)

1. Ensure your **Binance UM Futures** endpoint (or compatible gateway) is reachable via `BINANCE_BASE_URL` and **WS** via `BINANCE_WS_BASE`.

2. Bring up services:

```bash
docker compose build marketdata ema
docker compose up -d redis marketdata ema
```

3. Health & metrics:

```bash
curl -s http://localhost:9002/healthz
curl -s http://localhost:9001/healthz
curl -s http://localhost:9001/metrics | head
```

4. Verify Redis activity:

```bash
# Keys written by marketdata
docker compose exec redis sh -lc "redis-cli --scan --pattern 'candle:last:*' | head"
docker compose exec redis sh -lc "redis-cli GET candle:last:BTCUSDT:1h | head -c 200; echo"
```

5. Check orders/position (on your UM Futures endpoint):

```bash
# Open orders & position (Binance futures-style paths)
curl -s "${BINANCE_BASE_URL}/fapi/v1/openOrders?symbol=BTCUSDT"
curl -s "${BINANCE_BASE_URL}/fapi/v2/positionRisk?symbol=BTCUSDT"
```

---

## What each service does

### `runner/marketdata.py`

* Connects WS: `${BINANCE_WS_BASE}/stream`
* Subscribes: `${symbol}@kline_${TF_PRIMARY}` (+ mark price if enabled)
* On **closed** kline (`x=true`):

  * `SET candle:last:{SYMBOL}:{TF}` → raw kline JSON `{t,T,s,i,o,c,h,l,v,x:true,...}`
  * `PUBLISH candles:{SYMBOL}:{TF}` → same payload
* Exposes `GET /healthz`, `GET /metrics` (Prometheus)

### `runner/ema.py`

* **Polls** `candle:last:{SYMBOL}:{TF}` (no pub/sub in this branch)
* On new closed candle:

  * `EmaCrossStrategy` → `"LONG" | "SHORT" | "NONE"`
  * `EmaExecutor`:

    * places **MARKET** entry (sized from `ALLOC_USDT`)
    * places **reduce-only STOP_MARKET** exit at `STOP_PCT`
    * optional DB logging if `DB_ENABLED=true` and `DB_URL` is set
* Metrics: `bot_up{bot="<BOT_ID>"}` and `order_latency_ms_*` (REST ACK histogram)
* Exposes `GET /healthz`, `GET /metrics`

---

## Exchange adapter (UM Futures)

`common/exchange.py` uses **binance-connector**’s **UMFutures**:

* Futures REST paths `/fapi/v1/*`, `/fapi/v2/*`
* Attempts to enable **dual-side hedge mode**
* Supports **reduce-only** flags
* Caches `exchangeInfo` (filters like `stepSize`/`tickSize`)
* Retries suitable for bot workloads

---

## Trade logging (optional)

If you enable DB logging (`DB_ENABLED=true` + `DB_URL`):

* **Tables** (see `schemas.sql` and `storage/models.py`):

  * `strategy_configs`, `runs`, `orders`, `fills` are populated by the EMA runner.
  * `positions`, `ledger`, `metrics` are defined for legacy analytics but not
    written by this branch.
* Intended use: **auditing**, **PnL analysis**, offline **analytics** once the
  downstream loaders populate the legacy tables.

> There is **no HTTP/API logging sink** in this mainline; logging is DB-only when enabled.

---

## Roadmap (architecture)

* Keep this **single core** (marketdata + strategy runner + adapters).
* Add new strategies as **modules/plugins** reusing the same exchange/redis/storage interfaces.
* Optionally switch the EMA runner to **pub/sub** consumption (instead of polling) in a future iteration.

---

## Troubleshooting

* **404 `/fapi/v1/exchangeInfo`**
  Your `BINANCE_BASE_URL` is wrong or the target does not expose Binance **futures** paths. Point to UM Futures (or a compatible gateway).

* **No `candle:last:*` keys**
  Check WS base/protocol (`BINANCE_WS_BASE`), symbol/TF exist, and `marketdata` logs for WS errors.

* **EMA not trading**
  Ensure `SYMBOL`/`TF_PRIMARY` match between services; try small EMAs (`EMA_FAST=2`, `EMA_SLOW=4`) to force crosses; confirm `ALLOC_USDT`/`STOP_PCT`.

* **Metrics empty**
  Hit `/metrics` directly; verify `BOT_ID` label; check compose port mapping.

* **Linux & `host.docker.internal`**
  Use host IP or run your gateway/simulator inside the same compose.

---

## Notes

This mainline focuses on an **EMA cross** with simple sizing & stops. Funding filters, slippage caps, and capital-lock features are **not** enabled here. Use DB logging if you need post-trade analytics.
