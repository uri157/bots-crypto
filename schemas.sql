-- Tabla de órdenes enviadas
CREATE TABLE orders (
order_id TEXT PRIMARY KEY,
symbol TEXT,
side TEXT,
type TEXT,
qty DOUBLE,
price DOUBLE,
status TEXT,
etc.)
time TIMESTAMP,
client_id TEXT,
-- ID de orden (exchange)
-- 'BUY' o 'SELL'
-- 'MARKET', 'LIMIT', 'STOP'...
-- estado ('NEW','FILLED','CANCELED',
-- timestamp colocación
-- idempotency key (cliente)
53strategy TEXT
'momentum' o 'basis')
);
-- bot/estrategia origen (p.ej.
-- Tabla de ejecuciones (fills) de órdenes
CREATE TABLE fills (
fill_id TEXT PRIMARY KEY,
-- identificador de fill (puede ser
combo de order_id+fill_num)
order_id TEXT REFERENCES orders(order_id),
symbol TEXT,
qty DOUBLE,
price DOUBLE,
fee DOUBLE,
time TIMESTAMP
-- comisión pagada en USDT
);
-- Tabla de posiciones abiertas (hedge mode permite long y short separados)
CREATE TABLE positions (
symbol TEXT,
side TEXT,
-- 'LONG' o 'SHORT'
qty DOUBLE,
entry_price DOUBLE,
entry_time TIMESTAMP,
strategy TEXT,
PRIMARY KEY(symbol, side, strategy)
);
-- Tabla de ledger (eventos financieros: cierre de trade, funding realizado,
etc.)
CREATE TABLE ledger (
ledger_id UUID PRIMARY KEY DEFAULT generate_uuid(),
symbol TEXT,
strategy TEXT,
timestamp TIMESTAMP,
type TEXT,
-- 'TRADE' (PnL de trade cerrado), 'FUNDING', etc.
amount DOUBLE
-- monto en USDT (positivo = ganancia, negativo =
pérdida)
);
-- Tabla de métricas (opcional: registro histórico de métricas si se desea)
CREATE TABLE metrics (
ts TIMESTAMP,
name TEXT,
value DOUBLE
);
-- Índices para optimización de consultas frecuentes
CREATE INDEX idx_order_symbol ON orders(symbol);
CREATE INDEX idx_ledger_time ON ledger(timestamp);
CREATE INDEX idx_metrics_name ON metrics(name);