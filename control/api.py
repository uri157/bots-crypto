# common/locks.py
from __future__ import annotations

from typing import Optional
from redis.asyncio import Redis


class LockError(Exception):
    """Error al adquirir, renovar o liberar el lock."""


# Lua scripts para operaciones atómicas (check-and-*)
# RELEASE: borra sólo si el dueño coincide.
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
else
  return 0
end
"""

# RENEW: renueva TTL sólo si el dueño coincide (usa milisegundos).
_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
else
  return 0
end
"""


async def acquire_lock(redis: Redis, key: str, holder: str, ttl: int = 600) -> bool:
    """
    Intenta tomar el lock global (SET NX EX).
    Devuelve True si se adquirió, False si ya estaba tomado.
    """
    try:
        # redis-py devuelve True si se setea, False si no (con nx=True)
        ok = await redis.set(key, holder, nx=True, ex=ttl)
        return bool(ok)
    except Exception as e:
        raise LockError(f"Cannot acquire lock: {e}") from e


async def renew_lock(redis: Redis, key: str, holder: str, ttl: int = 600) -> bool:
    """
    Renueva el TTL sólo si `holder` es el dueño actual (operación atómica).
    Devuelve True si renovado, False si el lock ya no nos pertenece.
    """
    try:
        # ttl en milisegundos para mayor precisión
        res = await redis.eval(_RENEW_LUA, 1, key, holder, int(ttl * 1000))
        return int(res or 0) == 1
    except Exception as e:
        raise LockError(f"Cannot renew lock: {e}") from e


async def release_lock(redis: Redis, key: str, holder: str) -> None:
    """
    Libera el lock sólo si `holder` coincide (operación atómica).
    Silencioso si no somos dueños.
    """
    try:
        await redis.eval(_RELEASE_LUA, 1, key, holder)
    except Exception as e:
        raise LockError(f"Cannot release lock: {e}") from e


async def current_holder(redis: Redis, key: str) -> Optional[str]:
    """
    Devuelve el holder actual (str) o None si no existe.
    """
    try:
        val = await redis.get(key)
        if val is None:
            return None
        return val.decode() if isinstance(val, (bytes, bytearray)) else str(val)
    except Exception as e:
        raise LockError(f"Cannot read lock holder: {e}") from e
