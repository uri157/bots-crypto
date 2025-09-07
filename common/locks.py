// common/locks.py

import aioredis
class LockError(Exception):
"""Error al adquirir o mantener el lock de capital."""
async def acquire_lock(redis, key: str, holder: str, ttl: int = 600) -> bool:
"""Intenta adquirir el lock global de capital (clave `key`) para este bot
(`holder`).
Devuelve True si obtenido, False si ya está tomado.
Lanza LockError en caso de error de conexión."""
try:
# SET key = holder NX EX ttl
return await redis.set(key, holder, exist=redis.SET_IF_NOT_EXIST,
expire=ttl)
except Exception as e:
raise LockError(f"Cannot acquire lock: {e}")
async def renew_lock(redis, key: str, holder: str, ttl: int = 600) -> bool:
"""Renueva el lock si `holder` es el dueño actual. Devuelve True si
renovado, False si lock perdido."""
try:
current = await redis.get(key)
if current and current.decode() == holder:
# Reset expiry
await redis.expire(key, ttl)
return True
return False
except Exception as e:
raise LockError(f"Cannot renew lock: {e}")
async def release_lock(redis, key: str, holder: str) -> None:
"""Libera el lock si `holder` coincide. Ignora si no es dueño o ya
expiró."""
try:
val = await redis.get(key)
if val and val.decode() == holder:
await redis.delete(key)
except Exception as e:
raise LockError(f"Cannot release lock: {e}")