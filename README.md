# bots-crypto

Minimal **trading bots** stack in Docker.

> **Purpose:** Bots designed to operate against **Binance UM Futures**.
> They also work with **any Binance-compatible API** (same REST `/fapi/*` and WS `/stream`), including simulators—but that’s purely on the API provider side.

## Components

* **marketdata** — subscribes to Binance-style **WebSocket** klines/mark price and writes the **latest closed candle** to **Redis** (key + pub/sub).
* **ema** — EMA-cross strategy that consumes Redis **pub/sub**, sizes orders, places **MARKET** entries and reduce-only **STOP_MARKET** exits via a Binance adapter, and can **log trades** for analytics.
* *(optional)* **metrics** — Prometheus registry (if you prefer a central one).
* *(optional / WIP)* **control** — small HTTP API for orchestration / external logging sink.

Each service exposes **`/healthz`** and **`/metrics`** (Prometheus). Configuration is via **environment variables**.

---

## Topology

```
[Binance UM Futures]  ←─ WS/HTTP (Binance) ─→  [marketdata]
                                            │
                                            │  Redis
                                            ▼
                                   key:  candle:last:{SYMBOL}:{TF}
                                   chan: candles:{SYMBOL}:{TF}
                   HTTP (orders/info) ─────→  [ema]
                                               └─ trade logs →
                                                  [storage DB]  (default)
                                                  [external API] (alt, WIP)
```

---

## Repository layout (high-level)

```
bots-crypto/
├─ common/               # exchange adapter (Binance UMFutures), locks
├─ datafeed/             # WS client for klines/mark price, funding helpers
├─ ema/                  # EMA strategy (signals) + executor (orders/stops/logs)
├─ runner/               # service entries (marketdata, ema) with /healthz /metrics
├─ storage/              # DB helpers + schema (orders, fills, positions, ledger)
├─ metrics/              # optional central Prometheus registry
├─ control/              # (WIP) HTTP control / external log receiver
├─ docker-compose.yml    # redis + marketdata + ema (and optional daemons)
└─ .env.sample
```

---

## Docker services (default)

* `redis`
* `marketdata` (defaults to `:9002`)
* `ema` (defaults to `:9001`)

If you add optional daemons (metrics/control), include their ports in your compose.

---

## Configuration

Copy and edit defaults:

```bash
cp .env.sample .env
```

**Common**

* `REDIS_URL` — e.g. `redis://redis:6379/0`
* `LOG_LEVEL` — `INFO` or `DEBUG`

**Binance endpoints (choose one set):**

* **Production**
  `BINANCE_BASE_URL=https://fapi.binance.com`
  `BINANCE_WS_BASE=wss://fstream.binance.com`
* **Testnet**
  `BINANCE_BASE_URL=https://testnet.binancefuture.com`
  `BINANCE_WS_BASE=wss://stream.binancefuture.com`
  `TESTNET=true`
* **Any Binance-compatible API** (if you run one)
  `BINANCE_BASE_URL=http://host.docker.internal:9010`
  `BINANCE_WS_BASE=ws://host.docker.internal:9010`

**marketdata**

* `SYMBOLS=BTCUSDT` (comma-separated to track more)
* `TF_PRIMARY=1h`
* `HTTP_PORT=9002`

**ema**

* `BOT_ID=ema`
* Strategy & sizing: `EMA_FAST`, `EMA_SLOW`, `ALLOC_USDT`
* Stops & safety: `STOP_PCT`, `SLIPPAGE_CAP_BPS`, `CAPITAL_LOCK_KEY`
* Optional filters: `FUNDING_MAX_BPS`, `FUNDING_WINDOW_H`
* `HTTP_PORT=9001`

**Storage (trade logging, default)**

* `DATABASE_URL=postgresql://…` *(or DuckDB/SQLite — see `schemas.sql`)*

**External logging (WIP alternative)**

* `LOG_SINK=api`
* `LOG_API_BASE_URL=https://your-logger`
* `LOG_API_TOKEN=…`

---

## Run

```bash
docker compose build marketdata ema
docker compose up -d redis marketdata ema
```

Health & metrics:

```bash
curl -s http://localhost:9002/healthz
curl -s http://localhost:9001/healthz
curl -s http://localhost:9001/metrics | head
```

Redis activity:

```bash
docker compose exec redis sh -lc "redis-cli --scan --pattern 'candle:last:*' | head"
docker compose exec redis sh -lc "redis-cli GET candle:last:BTCUSDT:1h | head -c 200; echo"
docker compose exec redis sh -lc "redis-cli SUBSCRIBE candles:BTCUSDT:1h"
```

---

## What each service does

### `runner/marketdata.py`

* Connects WS: `${BINANCE_WS_BASE}/stream`
* Subscribes: `${symbol}@kline_${TF_PRIMARY}` (+ mark price if enabled)
* On **closed candle** (`x=true`):

  * sets `candle:last:{SYMBOL}:{TF}`
  * **publishes** to `candles:{SYMBOL}:{TF}` (Redis pub/sub)
* Exposes `GET /healthz`, `GET /metrics`

### `runner/ema.py`

* **Subscribes** to `candles:{SYMBOL}:{TF}` via Redis pub/sub
* On each closed candle:

  * `EmaCrossStrategy` → `"LONG" | "SHORT" | "NONE"`
  * `EmaExecutor` places **MARKET** entries, **reduce-only STOP_MARKET** exits, applies **slippage caps** and a Redis **capital lock**
  * **Logs trades** to DB (default) or **POSTs** to external API (WIP)
* Metrics: `bot_up{bot="ema"}`, `order_latency_ms_*` (histogram)

---

## Exchange adapter

`common/exchange.py` is an async adapter over **binance-connector UMFutures**:

* Caches `exchangeInfo` (filters like `stepSize`/`tickSize`)
* Places/cancels orders (MARKET, reduce-only **STOP_MARKET**)
* Reads account/position/funding
* Retries/backoff suitable for bots

---

## Trade logging (analytics)

* **Default sink: DB** (`DATABASE_URL`)
  Tables: `orders`, `fills`, `positions`, `ledger`, `metrics` (see `schemas.sql`).
* **Alternative sink (WIP): HTTP API**
  Set `LOG_SINK=api` and point to your receiver (`LOG_API_BASE_URL`).
  Keep `schemas.sql` as the field contract.

---

## Troubleshooting

* **404 on `/fapi/v1/exchangeInfo`** → Your `BINANCE_BASE_URL` isn’t a Binance-style API. Point it to Binance (prod/testnet) or a truly compatible API.
* **No `candle:last:*` keys** → Check WS base, symbol/interval, and `marketdata` logs.
* **No orders from `ema`** → Match `SYMBOL`/`SYMBOLS` and `TF_PRIMARY`; try small EMAs (`2/4`) to force crosses; verify `ALLOC_USDT`, `STOP_PCT`, `SLIPPAGE_CAP_BPS`.
* **DB connection refused** → Disable DB logging or fix `DATABASE_URL`.

---

## Disclaimer

This is **simulation/testing** software. Not financial advice. No performance guarantees.
