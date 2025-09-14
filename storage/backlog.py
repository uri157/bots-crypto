# storage/backlog.py
from __future__ import annotations
import json, os, tempfile, shutil
from typing import Callable, Dict, List, Any

class DiskBacklog:
    """
    Backlog JSONL en disco (append-only + compactación por batch).
    - append(event): agrega un evento {dict} como línea JSON.
    - drain(process_fn, max_batch): lee todo el archivo, intenta procesar
      cada evento; si falla, lo conserva; reescribe el archivo sólo con
      los que fallaron (compactación).
    """
    def __init__(self, path: str = "/app/data/db_backlog.jsonl") -> None:
        self.path = path
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # crea si no existe
        if not os.path.exists(self.path):
            open(self.path, "a").close()

    def append(self, event: Dict[str, Any]) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, separators=(",", ":")) + "\n")
        except Exception:
            # best effort: si incluso esto falla, no bloqueamos el bot
            pass

    def drain(self, process_fn: Callable[[Dict[str, Any]], None], max_batch: int = 500) -> int:
        """
        Intenta procesar hasta max_batch eventos. Devuelve la cantidad
        efectivamente procesada. Los eventos que fallen se conservan.
        """
        processed = 0
        keep: List[str] = []
        tmpfile = None

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return 0  # no bloqueamos

        for line in lines[:max_batch]:
            s = line.strip()
            if not s:
                continue
            try:
                ev = json.loads(s)
            except Exception:
                # línea corrupta → descartamos
                continue
            try:
                process_fn(ev)
                processed += 1
            except Exception:
                # no se pudo escribir → conservar
                keep.append(s + "\n")

        # si hay más líneas sin procesar (después del batch), conservarlas
        keep.extend(lines[max_batch:])

        # compactar
        try:
            fd, tmpfile = tempfile.mkstemp(prefix="backlog_", suffix=".jsonl",
                                           dir=os.path.dirname(self.path))
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                out.writelines(keep)
            shutil.move(tmpfile, self.path)
        except Exception:
            # si la compactación falla, intentamos no corromper
            if tmpfile and os.path.exists(tmpfile):
                try:
                    os.remove(tmpfile)
                except Exception:
                    pass

        return processed
