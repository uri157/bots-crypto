# control/api.py
from __future__ import annotations

from typing import Optional, Callable, Any, Dict
from redis.asyncio import Redis

# ==========================
#  Locks atómicos en Redis
# ==========================

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


# ==========================
#  Control API (FastAPI)
# ==========================

# Nota:
# - Los runners importan:  from control.api import create_control_api
# - Esta función devuelve una app FastAPI con endpoints:
#   /healthz, /status, /pause, /resume, /close_all, GET/POST /mode
#
# Uso flexible:
#   create_control_api(obj)                           # obj expone status/pause/resume/close_all/get_mode/set_mode
#   create_control_api(status=..., pause=..., ...)    # o pasar handlers por kwargs
#
# Cada handler es opcional: si falta, el endpoint responde algo por defecto.

from fastapi import FastAPI, Body


def create_control_api(*args: Any, **kwargs: Any) -> FastAPI:
    """Crea y retorna una app FastAPI con endpoints de control básicos."""
    # Normaliza handlers desde kwargs o desde el primer argumento posicional (obj)
    handlers: Dict[str, Callable[..., Any]] = {}
    handlers.update({k: v for k, v in kwargs.items() if callable(v)})

    if args:
        obj = args[0]
        # Si el objeto trae métodos con estos nombres, los usamos.
        for name in ("status", "pause", "resume", "close_all", "get_mode", "set_mode"):
            if name not in handlers and hasattr(obj, name) and callable(getattr(obj, name)):
                handlers[name] = getattr(obj, name)

        # Si el objeto tiene un atributo 'mode', lo exponemos por GET /mode si no hay get_mode
        if "get_mode" not in handlers and hasattr(obj, "mode"):
            def _get_mode_attr() -> Any:
                return getattr(obj, "mode")
            handlers["get_mode"] = _get_mode_attr  # type: ignore[assignment]

        if "set_mode" not in handlers and hasattr(obj, "mode"):
            def _set_mode_attr(mode: str) -> None:
                setattr(obj, "mode", mode)
            handlers["set_mode"] = _set_mode_attr  # type: ignore[assignment]

    def _call(name: str, *a: Any, **k: Any) -> Any:
        fn = handlers.get(name)
        return fn(*a, **k) if callable(fn) else None

    app = FastAPI(title="Control API", version="0.1.0")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/status")
    def status():
        res = _call("status")
        return res if res is not None else {"status": "unknown"}

    @app.post("/pause")
    def pause():
        _call("pause")
        return {"ok": True}

    @app.post("/resume")
    def resume():
        _call("resume")
        return {"ok": True}

    @app.post("/close_all")
    def close_all():
        _call("close_all")
        return {"ok": True}

    @app.get("/mode")
    def get_mode():
        res = _call("get_mode")
        return {"mode": res if res is not None else "unknown"}

    @app.post("/mode")
    def set_mode(mode: str = Body(embed=True)):
        _call("set_mode", mode)
        return {"ok": True, "mode": mode}

    return app
