import os, asyncio, json, aiohttp
from redis.asyncio import Redis
async def main():
    r = Redis.from_url(os.getenv("REDIS_URL","redis://redis:6379/0"))
    base = os.getenv("BINANCE_WS_BASE","ws://host.docker.internal:9010")
    url  = f"{base}/stream?streams=btcusdt@kline_1h/btcusdt@markPrice@1s"
    print("connecting to", url, flush=True)
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url, heartbeat=30) as ws:
            for _ in range(50):  # lee 50 mensajes y sale
                msg = await ws.receive()
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                d = json.loads(msg.data); data = d.get("data", {}); evt = data.get("e"); sym = data.get("s")
                if evt == "markPriceUpdate" and sym:
                    p = data.get("p")
                    await r.set(f"mark:last:{sym}", str(p), ex=900)
                    await r.set(f"price:last:{sym}", str(p), ex=900)
                    print("mark", sym, p, flush=True)
                elif evt == "kline" and data.get("k",{}).get("x") and sym:
                    k = data["k"]; iv = k.get("i","?")
                    await r.set(f"candle:last:{sym}:{iv}", json.dumps(k), ex=900)
                    print("kline", sym, iv, flush=True)
asyncio.run(main())
