from datetime import datetime, timezone, timedelta

def within_no_trade_window(window_s: int, now: datetime|None=None) -> bool:
    if window_s <= 0:
        return False
    now = (now or datetime.utcnow()).replace(tzinfo=timezone.utc)
    cuts = []
    for base in (now - timedelta(days=1), now, now + timedelta(days=1)):
        for h in (0,8,16):
            cuts.append(base.replace(hour=h, minute=0, second=0, microsecond=0))
    return any(abs((now - c).total_seconds()) <= window_s for c in cuts)
