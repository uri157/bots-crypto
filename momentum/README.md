# Momentum — Bot de Momentum sobre Perps (USDⓈ‑M)

Este módulo ejecuta una **estrategia de momentum** sobre contratos perpetuos (p. ej. `BTCUSDT`) conectándose a un **gateway Binance‑like** (ExSim) y usando **Redis** como bus de datos/locks. Incluye ejecución de entradas a mercado + **STOP\_MARKET reduce‑only**, trailing por **ATR**, filtros de **funding**, ventanas de no‑trade alrededor de cortes (00/08/16 UTC) y control de capital.

> Diseñado para correr en Docker Compose junto a `marketdata` (WS), `riskd`, `basis` y el **exchange‑simulator**.

---

## Árbol y responsabilidades

```
momentum/
├─ services/
│  ├─ funding.py     # Lógica de umbrales/percentiles de funding y lectura desde Redis
│  ├─ orders.py      # Wrappers a Exchange: MARKET, STOP_MARKET reduce‑only, cancelaciones seguras
│  └─ stops.py       # Gestión y trailing del STOP (cancel+recrea), IDs de stop, reduce_only
├─ utils/
│  ├─ atr.py         # ATR actual + fallback por porcentaje configurable del precio
│  ├─ prices.py      # Lectura de últimos precios (Redis: price:last:*, mark:last:*) con caché local
│  └─ time_windows.py# Ventanas de no‑trade alrededor de los funding cuts (00/08/16 UTC)
├─ config.py         # Lectura/normalización de ENV (slippage, límites, ventanas, etc.)
├─ executor.py       # Orquestador: hooks de mercado, open_position, trailing, lock de capital
└─ strategy.py       # Interfaz y referencia de estrategia de momentum (señales + ATR)
```

### executor.py (núcleo)

* **Hooks**:

  * `on_new_candle(close, high, low)`: actualiza indicadores y decide **entradas** si pasa filtros.
  * `on_price_update(last_price)`: mueve el **trailing stop** con base en ATR.
* **Ejecución**:

  * Entrada **MARKET** + creación de **STOP\_MARKET reduce\_only** opuesto (SL).
  * **Slippage cap** configurable: si el fill se desvía > cap vs. último precio cacheado → rollback.
  * **Trailing**: `ATR_TRAIL_MULT * ATR` (
    LONG: sube stop; SHORT: baja stop).
* **Control**:

  * `capital:lock` en Redis: si el lock no es del bot → `pause_and_drain()`.
  * `LOSS_STREAK_LIMIT` pausa tras rachas de pérdidas (p\&l reportado por `riskd`).
* **Estado**: `active_position` (`side, entry_price, qty, stop_price, stop_order_id`).

### strategy.py

Interfaz mínima esperada por el executor:

```py
class MomentumStrategy:
    symbol: str
    def update_candle(self, close: float, high: float, low: float) -> None: ...
    def generate_signal(self, funding_rate_h: float, long_max: float, short_min: float) -> tuple[str, float, float]: ...
    def get_current_atr(self) -> Optional[float]: ...
    def get_last_close(self) -> Optional[float]: ...
```

Implementación de referencia: cálculos de ATR y ruptura/tendencia simple para producir `(signal, qty, stop_price)`.

### services/

* **orders.py**: helpers de **place\_order** (MARKET/STOP\_MARKET), `cancel_order_safe`, reduce\_only.
* **stops.py**: mover stop (cancel + recrea), client IDs, actualización de `active_position`.
* **funding.py**: lee `funding:{SYMBOL}` (decimal/hora) y opcionalmente percentiles `funding:pctl:*`.

### utils/

* **atr.py**: `get_current_atr()` y `_get_atr_fallback(symbol, last_price)` → usa envs `ATR_MIN_PCT_*`.
* **prices.py**: cachea `price:last:{SYMBOL}` de Redis y hint de precio del último fill.
* **time\_windows.py**: `within_no_trade_window(±N s)` para cortes 00/08/16 UTC.

---

## Dependencias externas

* **Exchange** (`common.exchange.Exchange`): adaptador async sobre `binance-connector` (UMFutures) con **overrides** a `BINANCE_BASE_URL`/`BINANCE_WS_BASE` → apunta al **exchange‑simulator**.
* **Redis**: feed de precios/klines/funding y **lock de capital**.
* **Exchange‑simulator (ExSim)**: REST/WS estilo Binance (subset). Endpoints usados: `/fapi/v1/order`, `/openOrders`, `/fapi/v1/exchangeInfo`, `/fapi/v1/premiumIndex`, `/fapi/v2/positionRisk`, `/fapi/v2/balance`, WebSocket `/stream` (kline + markPrice).

---

## Variables de entorno relevantes (momentum)

* **Símbolo/TF**: `SYMBOLS` (p. ej. `BTCUSDT`), `TF_PRIMARY` (p. ej. `1h`), `TF_SECONDARY`.
* **Sizing/Riesgo**: `EQUITY_USDT`, `RISK_PCT_PER_TRADE`, `MARGIN_BUFFER_MIN`, `LOSS_STREAK_LIMIT`, `DAILY_MAX_DD`, `MAX_OPEN_ORDERS`, `POSITION_MODE`, `BINANCE_DUAL_SIDE`.
* **Momentum/ATR**: `ATR_STOP_MULT`, `ATR_TRAIL_MULT`, `ATR_MIN_PCT_DEFAULT`, `ATR_MIN_PCT_BTC`, `ATR_MIN_PCT_ETH`.
* **Funding**: `FUNDING_LONG_MAX`, `FUNDING_SHORT_MIN`, `USE_FUNDING_PERCENTILES`, `FUNDING_PCTL_LOOKBACK_DAYS`, `FUNDING_LONG_MAX_PCTL`, `FUNDING_SHORT_MIN_PCTL`, `NO_TRADE_FUNDING_WINDOW_S`, `LOG_REALIZED_FUNDING`.
* **Infra**: `WS_MAX_DOWNTIME_S`, `ORDER_LATENCY_MAX_MS`, `ORDER_TIMEOUT_MS`, `TIME_SYNC_SKEW_MS`.
* **Identidad/flags**: `BOT_ID` (p. ej. `momentum`), `CAPITAL_LOCK_KEY` (p. ej. `capital:lock:mm`), `ENABLE_MOMENTUM=true`.
* **Exchange**: `BINANCE_BASE_URL`, `BINANCE_WS_BASE` (apuntar al ExSim), `MODE=paper`.
* **Otros**: `SLIPPAGE_CAP` (p. ej. `0.002` = 20 bps).

> **.env de ejemplo** está en la raíz del proyecto. El bot sólo entra si posee el lock: `redis set capital:lock:mm momentum`.

---

## Redis — claves relevantes

* **Precios**: `price:last:{SYMBOL}`, `mark:last:{SYMBOL}`.
* **Klines**: `candle:last:{SYMBOL}:{TF}` (JSON con `o,h,l,c`, `x=true` al cerrar).
* **Funding**: `funding:{SYMBOL}` (decimal/h), percentiles opcionales `funding:pctl:long:{SYMBOL}`, `funding:pctl:short:{SYMBOL}`.
* **Control**: `capital:lock` o `capital:lock:mm` → valor debe ser `BOT_ID`.
* **Eventos**: stream `events` (el executor publica `drained`).

---

## Métricas Prometheus (`/metrics`)

* `bot_up{bot="momentum"}`
* `order_latency_ms` (histograma)
* `pnl_day_usdt` (gauge; agregado por `riskd`)
* (futuros) fills/fees/posición activa

---

## Flujo de datos (resumen)

1. `marketdata` se conecta por WS a ExSim y publica en Redis:

   * `price:last:*`, `mark:last:*`, `candle:last:*`.
2. `momentum` lee velas cerradas (`on_new_candle`) y ticks (`on_price_update`).
3. `executor` decide entrada → `Exchange.place_order(MARKET)` y crea **STOP\_MARKET reduce\_only**.
4. Trailing periódicamente ajusta el stop (cancel + nueva orden) con `ATR_TRAIL_MULT`.
5. Control de lock/funding/windows evita operar fuera de condiciones.

---

## Puesta en marcha rápida (Docker Compose)

```bash
# Levantar servicios
sudo docker compose up -d

# Verificar env del bot
sudo docker compose exec momentum sh -lc 'echo $BINANCE_BASE_URL $BINANCE_WS_BASE $MODE $ENABLE_MOMENTUM $BOT_ID $CAPITAL_LOCK_KEY'

# Dar lock al bot
sudo docker compose exec redis sh -lc "redis-cli set capital:lock:mm momentum"

# Hacer replay rápido desde ExSim
curl -s -X POST 'http://localhost:9010/admin/replay' \
  -H 'Content-Type: application/json' \
  -d '{"speed_bars_per_sec":200}'

# Chequeos
curl -s http://localhost:9010/admin/status | jq '.ws_clients,.symbol,.interval,.bars_loaded'
redis-cli --scan --pattern 'price:last:*'
redis-cli --scan --pattern 'candle:last:*'
```

### Smoke tests de órdenes (ExSim)

```bash
# STOP_MARKET manual
curl -s -X POST http://localhost:9010/fapi/v1/order \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"BTCUSDT","side":"SELL","type":"STOP_MARKET","stopPrice":"99999","quantity":"0.001","newClientOrderId":"stop-mkt-smoke"}' | jq

# Órdenes abiertas
curl -s 'http://localhost:9010/fapi/v1/openOrders?symbol=BTCUSDT' | jq
```

### Métricas / estado

```bash
curl -s http://localhost:9001/metrics | egrep -i 'bot_up|order_latency'
curl -s 'http://localhost:9010/fapi/v2/positionRisk?symbol=BTCUSDT' | jq
curl -s 'http://localhost:9010/fapi/v2/balance' | jq
```

---

## Backtest largo (via ExSim CLI)

Ejecutar el gateway ExSim contra DuckDB en local y streamear **rápido** un rango amplio:

```bash
python -m gateway.main \
  --duckdb-path data/duckdb/exsim.duckdb \
  --symbol BTCUSDT --interval 1h \
  --start 2024-01-01 --end 2024-12-31 \
  --speed 250 --port 9010
```

> El bot seguirá consumiendo por WS/REST y operando según reglas.

---

## Troubleshooting

* **Unsupported order type (-1116)**: asegura que ExSim tenga soporte para `STOP_MARKET` y alias `orderType`→`STOP_MARKET` mapeado.
* **`CancelledError` repetido**: suele indicar reinicios/replay; es benigno si el servicio sigue vivo.
* **`Connection refused host.docker.internal:9010`**: ExSim no está levantado o no expuesto a Docker; confirma `BINANCE_BASE_URL`/`WS_BASE` y `curl` desde el contenedor del bot.
* **`exchangeInfo 500 / DuckDB InvalidInputException`**: revisar script de carga/consulta en ExSim y que DuckDB no tenga queries pendientes.

---

## Roadmap

* Persistir órdenes/positions en DuckDB desde el bot (para informes de rendimiento).
* WS de user‑data simulado (`executionReport`).
* Dual‑side real por símbolo.
* Métricas adicionales (fills, fees, trailing updates, slippage real vs. cap).
* Tests unitarios de services/utils.

---

## Licencia y alcance

Uso **exclusivo para desarrollo/backtesting**. No apto para trading real sin auditoría adicional, controles de riesgo y firma/autenticación.
