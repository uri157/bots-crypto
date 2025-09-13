# bots-crypto — README para IA (y humanos)

> Sistema minimalista de **trading bots** en Docker compuesto por dos servicios:
> **marketdata** (streaming → Redis) y **ema** (estrategia EMA-cross que opera
> contra un *exchange simulator* estilo Binance). Incluye **métricas Prometheus**,
> health checks y configuración por **variables de entorno**.

Este README está pensado para que una IA (o cualquier persona) pueda:

* Entender **qué hace** cada componente y **cómo interactúan**.
* **Levantarlos por primera vez** en una PC con Docker.
* **Diagnosticar** problemas típicos (no hay velas, no hay órdenes, etc.).
* Tener a mano la **API interna** (clases/métodos principales) para implementar cambios.

---

## Visión general

```
[exchange-simulator]  ←─────(HTTP/WS estilo Binance)─────→  [marketdata]
       ↑                                                       │
       │   /admin/replay (reproduce histórico)                 │ Redis
       │                                                       ▼
       │                                                key: candle:last:{SYMBOL}:{TF}
       │                                                       │
       └─────────────────────────────(HTTP estilo Binance)────→│
                                                               ▼
                                                            [ema]
                                                       estrategia + executor
                                                       place_order/cancel→sim
```

* **exchange-simulator** (proyecto aparte) sirve datos históricos por REST/WS con rutas parecidas a Binance (`/fapi/*`, `/stream`, `/admin/replay`).
* **marketdata** se suscribe a WS de klines y va guardando la **última vela cerrada** de cada símbolo/TF en Redis.
* **ema** *pulsea* Redis; cuando detecta **cierre de vela nueva**:

  * actualiza EMAs,
  * decide señal **LONG/SHORT/NONE**,
  * coloca órdenes **MARKET** y un **STOP\_MARKET reduceOnly** si corresponde,
  * mide **latencia** de órdenes y exporta métricas.

---

## Estructura del repo

```
bots-crypto/
├─ common/
│  └─ exchange.py           # Adaptador HTTP estilo Binance (place_order, cancel, etc.)
├─ datafeed/
│  ├─ market_ws.py          # Cliente WS (klines/markPrice)
│  └─ funding.py            # (cálculos de funding/basis si se activan)
├─ ema/
│  ├─ config.py             # Lectura de env vars y defaults para el bot EMA
│  ├─ strategy.py           # EmaCrossStrategy (cálculo de EMAs y señales)
│  ├─ executor.py           # EmaExecutor (traduce señales a órdenes)
│  ├─ metrics/
│  │  └─ server.py          # (opcional) registro central de métricas
│  └─ utils/, services/     # utilitarios varios
├─ runner/
│  ├─ marketdata.py         # Entry del servicio marketdata (WS→Redis + /healthz + /metrics)
│  └─ ema.py                # Entry del servicio ema (poll Redis + estrategia + /healthz + /metrics)
├─ storage/                 # (reservado; hoy no se persiste aquí)
├─ docker-compose.yml       # Orquestación de redis + marketdata + ema
├─ Dockerfile               # Imagen base (ambos servicios comparten)
├─ requirements.txt
├─ schemas.sql              # (si aplica en el futuro)
└─ .env.sample              # Ejemplo de config
```

---

## Componentes y responsabilidades

### 1) `runner/marketdata.py`

* Se conecta al **WS** del exchange-simulator (ruta estilo Binance: `/stream`).
* Suscribe a `btcusdt@kline_1h` (y opcionalmente otros streams).
* Cada vez que llega una **vela cerrada** (`x=true`), publica en Redis:

  * **Key** `candle:last:{SYMBOL}:{TF}` (ej. `candle:last:BTCUSDT:1h`)
  * **Value (JSON)**: `{ t, T, s, i, o, c, h, l, v, x:true, ... }`
* Expone:

  * `GET /healthz` → `{"status":"OK"}`
  * `GET /metrics` → Prometheus (si `ema.metrics.server` no está presente, usa registry local)

### 2) `runner/ema.py`

* Hace **poll** de la key de Redis anterior (cada \~200ms).
* Al detectar un **cambio de `t` con `x=true`**:

  * Llama a `EmaCrossStrategy.generate_signal(close)`.
  * Si hay señal:

    * **`EmaExecutor`** abre/cierra posición con órdenes REST al exchange-simulator:

      * `BUY|SELL MARKET` (cantidad ≈ `ALLOC_USDT / price`, ajustada por `stepSize`)
      * `STOP_MARKET reduceOnly` (si `STOP_PCT > 0`)
    * Mide y expone **order\_latency\_ms** (histograma Prometheus).
* Expone:

  * `GET /healthz` → `{"ok":true}`
  * `GET /metrics` → Prometheus (`bot_up{bot="ema"}` y `order_latency_ms_*`)

### 3) `ema/strategy.py` (API principal)

```python
class EmaCrossStrategy:
    def __init__(self, symbol: str, fast: int, slow: int) -> None: ...
    def generate_signal(self, close: float) -> str:  # "LONG" | "SHORT" | "NONE"
        # actualiza EMAs internamente y compara (ema_fast - ema_slow)
        # cruce alcista: diff pasó de <=0 a >0 → "LONG"
        # cruce bajista: diff pasó de >=0 a <0 → "SHORT"
```

* Mantiene EMAs internas con suavizado estándar `α = 2 / (period+1)`.

### 4) `ema/executor.py` (API principal)

```python
class EmaExecutor:
    async def on_candle_close(self, close: float) -> None:
        signal = self.strategy.generate_signal(close)
        if signal == "LONG":  await self._ensure_long(close)
        elif signal == "SHORT":
            if self.cfg.allow_shorts: await self._ensure_short(close)
            else: await self._close_position(close)

    # Calcula qty en base a ALLOC_USDT y stepSize del símbolo
    async def _qty_from_alloc(self, price: float) -> float: ...

    # Arma STOP_MARKET reduceOnly según STOP_PCT y lado actual
    def _stop_from_pct(self, side: str, ref_price: float) -> Optional[float]: ...

    # place_order/cancel_order usan common.exchange.Exchange (HTTP estilo Binance)
```

### 5) `common/exchange.py`

* Pequeño cliente HTTP que mapea llamados estilo Binance:

  * `GET /fapi/v1/exchangeInfo` (para `stepSize`)
  * `POST /fapi/v1/order` (MARKET y STOP\_MARKET)
  * `DELETE /fapi/v1/order` (cancel)
  * `POST /fapi/v1/positionSide/dual?dualSidePosition=True` (modo BOTH)
* Funciona sobre `BINANCE_BASE_URL` (normalmente el simulator corriendo en `localhost:9010`).

---

## Configuración (variables de entorno)

> Mirá `./.env.sample` para valores por defecto. Copiá a `.env` y ajustá.

**Comunes**

* `REDIS_URL` → `redis://redis:6379/0`
* `LOG_LEVEL` → `INFO` (o `DEBUG`)
* `BINANCE_BASE_URL` → `http://host.docker.internal:9010`
* `BINANCE_WS_BASE` → `ws://host.docker.internal:9010`  *(marketdata)*

**marketdata**

* `SYMBOLS` → `BTCUSDT` (coma-separados si quisieras varios)
* `TF_PRIMARY` → `1h`
* `HTTP_PORT` → `9002`

**ema**

* `SYMBOL` → `BTCUSDT`
* `TF_PRIMARY` → `1h`
* `EMA_FAST` / `EMA_SLOW` → por ejemplo `2` y `4`
* `ALLOC_USDT` → p.ej. `1000`
* `STOP_PCT` → p.ej. `0.01` (1%)
* `ALLOW_SHORTS` → `true|false`
* `HTTP_PORT` → `9001`
* `BOT_ID` → `ema` (solo etiqueta para métricas)

> **Nota Linux**: `host.docker.internal` funciona en Docker moderno. Si no, reemplazalo por la IP del host (o corre el simulator como otro servicio en el mismo compose).

---

## Despliegue rápido (Docker)

1. **Levantá el exchange-simulator** (repo aparte) con datos cargados en DuckDB.

   * Ejemplo:

     ```bash
     # en el proyecto exchange-simulator (no este)
     python -m gateway.main \
       --duckdb-path data/duckdb/exsim.duckdb \
       --symbol BTCUSDT --interval 1h \
       --start 2024-01-01 --end 2024-12-31 \
       --speed 25 --maker-bps 1 --taker-bps 2 \
       --port 9010
     ```
   * Asegurate de tener soporte WS: `pip install "uvicorn[standard]"` (o `websockets`/`wsproto`).

2. **En este repo (`bots-crypto`)**:

   ```bash
   cp .env.sample .env   # editar si hace falta
   docker compose build ema marketdata
   docker compose up -d redis marketdata ema
   ```

3. **Arrancá la “reproducción” del histórico** en el simulator:

   ```bash
   curl -s -X POST http://localhost:9010/admin/replay \
     -H 'Content-Type: application/json' \
     -d '{"speed_bars_per_sec":25}'
   ```

4. **Checks básicos**

   ```bash
   # servicios vivos
   curl -s http://localhost:9002/healthz     # marketdata
   curl -s http://localhost:9001/healthz     # ema

   # ¿marketdata está escribiendo velas?
   docker compose exec redis sh -lc "redis-cli --scan --pattern 'candle:last:*' | head"
   docker compose exec redis sh -lc "redis-cli GET candle:last:BTCUSDT:1h | head -c 200; echo"

   # ¿ema expone métricas?
   curl -s http://localhost:9001/metrics | head

   # ¿hubo órdenes/posición en el simulator?
   curl -s 'http://localhost:9010/fapi/v1/openOrders?symbol=BTCUSDT' | jq
   curl -s 'http://localhost:9010/fapi/v2/positionRisk?symbol=BTCUSDT' | jq
   ```

---

## Métricas exportadas (Prometheus)

* `bot_up{bot="ema"}`: 1 si el proceso está activo.
* `order_latency_ms_*`: histograma de latencia REST de órdenes.

  * `order_latency_ms_count` → cantidad de órdenes observadas.
  * `order_latency_ms_sum` → suma de latencias en ms.
    Promedio ≈ `sum / count`.
* (Si usás `ema/metrics/server.py` central) hay *gauges/counters* adicionales:
  `ws_uptime_s`, `pnl_day_usdt`, `winrate_7d`, `fees_usdt`, `slippage_bps`,
  `funding_now_bps{symbol}`, `basis_now_pct{symbol}`, `margin_ratio`,
  `rejected_orders_count`.

---

## Comportamiento de trading (EMA)

* **Señal LONG**: `ema_fast` cruza **por encima** de `ema_slow`.
* **Señal SHORT**: `ema_fast` cruza **por debajo** de `ema_slow`.
* **Ejecución**:

  * Abre posición con **MARKET** (`qty ≈ ALLOC_USDT / price`, ajustado a `stepSize`).
  * Coloca **STOP\_MARKET reduceOnly** a `STOP_PCT` de distancia (si `STOP_PCT>0`).
  * Si llega señal contraria:

    * Cierra la posición actual con **MARKET** (y cancela stop anterior).
    * Si `ALLOW_SHORTS=true`, gira a short; si no, queda flat.

> El **estado de posición** vive en memoria del proceso EMA (no persiste).
> El gateway “simula” ejecución de órdenes y reporta `positionRisk` y `openOrders`.

---

## Problemas comunes (y pistas)

* **`/metrics` muestra `order_latency_ms_count 0`**
  No hubo órdenes aún. Acelerar el replay o usar ventanas EMA muy chicas (p.ej. 2/4) para forzar cruces.

* **`redis-cli --scan` no devuelve `candle:last:*`**
  marketdata no está sustituyendo WS → revisar:

  * `BINANCE_WS_BASE` debe ser `ws://...`
  * El simulator debe tener **WS habilitado** (`uvicorn[standard]`).
  * Logs de `marketdata` por 404 en `/stream`.

* **`ema` no opera** (pero hay velas)

  * Confirmar que `TF_PRIMARY` coincide (`1h` en ambos).
  * `SYMBOL` vs `SYMBOLS`.
  * Probar con `EMA_FAST=2, EMA_SLOW=4`.

* **Linux y `host.docker.internal`**
  Si no resuelve, usar IP del host o correr el simulator en el mismo `docker-compose` y conectar por nombre de servicio.

---

## Glosario mínimo

* **Latencia (order\_latency\_ms)**: tiempo (ms) entre `place_order` y la **respuesta** HTTP del “exchange”. No es el tiempo de llenado del mercado; es el *ACK* del endpoint.
* **STOP\_MARKET reduceOnly**: orden de stop que solo **reduce** posición (no abre una nueva).
* **stepSize**: granularidad mínima de cantidad (p.ej. 0.0001 BTC).

---

## Puntos de extensión

* **Estrategias**: agregá un nuevo archivo en `ema/strategy.py` o módulo aparte con la misma interfaz (`generate_signal(close: float) -> str`).
* **Ejecutor**: `ema/executor.py` concentra la lógica de qty, stops y giro.
* **Datos**: `datafeed/` permite añadir fuentes o enriquecer streams.
* **Métricas**: si existe `ema/metrics/server.py`, los servicios las usan; si no, cada runner expone una registry local.

---

## Chequeos útiles (copiar/pegar)

```bash
# Promedio de latencia de órdenes (aprox.)
curl -s http://localhost:9001/metrics | awk '/^order_latency_ms_(sum|count)/{print $1,$2}'

# Última vela guardada en Redis
docker compose exec redis sh -lc "redis-cli GET candle:last:BTCUSDT:1h | sed -e 's/,\"/,\n\"/g' | head -n 20"

# Estado del simulator
curl -s 'http://localhost:9010/fapi/v2/positionRisk?symbol=BTCUSDT' | jq
curl -s 'http://localhost:9010/fapi/v1/openOrders?symbol=BTCUSDT' | jq

# Forzar replay a 25 velas/seg
curl -s -X POST http://localhost:9010/admin/replay -H 'Content-Type: application/json' -d '{"speed_bars_per_sec":25}'
```

---

## Requisitos

* Docker y docker-compose (para este repo).
* Python 3.11+ si corrés sin Docker (no recomendado).
* Un **exchange-simulator** corriendo en el host (o accesible en red) con DuckDB poblado.

---

## Licencia / disclaimers

Este repo es para **simulación y pruebas**. No es asesoramiento financiero ni garantía de desempeño en mercados reales.

---

Si necesitás que adapte este bot a otra estrategia, o que le agregue trailing stops/TP, logging de fills a base, o soporte de varios símbolos/TF, la arquitectura ya está lista para enchufarlo sin romper lo demás.
