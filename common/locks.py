# common/locks.py
from __future__ import annotations

from typing import Any
from redis.asyncio import Redis


class LockError(Exception):
    """Error al adquirir o mantener el lock de capital."""


# Lua scripts para operaciones atómicas
_LUA_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
else
    return 0
end
"""

_LUA_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


async def acquire_lock(redis: Redis, key: str, holder: str, ttl: int = 600) -> bool:
    """
    Intenta adquirir el lock global `key` para este bot (`holder`).
    Devuelve True si se obtuvo, False si ya está tomado.
    Lanza LockError en caso de error de conexión.
    """
    try:
        # SET key value NX EX ttl  (atómico)
        ok: Any = await redis.set(name=key, value=holder, nx=True, ex=ttl)
        return bool(ok)
    except Exception as e:
        raise LockError(f"Cannot acquire lock: {e}") from e


async def renew_lock(redis: Redis, key: str, holder: str, ttl: int = 600) -> bool:
    """
    Renueva el lock si `holder` es el dueño actual (atómico).
    Devuelve True si renovado, False si lock perdido o distinto dueño.

    Nota: redis-py 5 usa firma posicional en EVAL:
      eval(script, numkeys, *keys_y_args)
    """
    try:
        ttl_ms = int(ttl * 1000)
        # keys=[key], args=[holder, ttl_ms]  -> firma posicional
        res: Any = await redis.eval(_LUA_RENEW, 1, key, holder, ttl_ms)
        # PEXPIRE devuelve 1 si éxito, 0 si no
        return bool(int(res or 0))
    except Exception as e:
        raise LockError(f"Cannot renew lock: {e}") from e


async def release_lock(redis: Redis, key: str, holder: str) -> None:
    """
    Libera el lock si `holder` coincide con el dueño actual (atómico).
    No falla si el lock ya expiró o pertenece a otro.

    Nota: redis-py 5 usa firma posicional en EVAL:
      eval(script, numkeys, *keys_y_args)
    """
    try:
        # keys=[key], args=[holder] -> firma posicional
        await redis.eval(_LUA_RELEASE, 1, key, holder)
    except Exception as e:
        raise LockError(f"Cannot release lock: {e}") from e
