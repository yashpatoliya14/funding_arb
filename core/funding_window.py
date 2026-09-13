"""
Requirement #9: check ~20 min before funding, close immediately after the
funding window passes.

Delta's funding snapshot happens at FIXED clock times (05:30, 13:30, 21:30
IST) as of the Sep-2025 schedule change — see constants.py. We use these as
the default schedule for all three exchanges; if CoinSwitch/Shark diverge for
a given symbol, override via their instrument-info endpoint (TODO markers
left in each client).
"""

from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from config.constants import (
    DELTA_FUNDING_TIMES_IST, ENTRY_LEAD_MINUTES, POST_SNAPSHOT_CLOSE_DELAY_SEC,
)

IST = ZoneInfo("Asia/Kolkata")


def _today_candidates(now_ist: datetime, times_str: list[str]) -> list[datetime]:
    candidates = []
    for t in times_str:
        h, m = map(int, t.split(":"))
        candidates.append(now_ist.replace(hour=h, minute=m, second=0, microsecond=0))
    return candidates


def next_funding_time(now: datetime = None, times_str: list[str] = None) -> datetime:
    times_str = times_str or DELTA_FUNDING_TIMES_IST
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    todays = _today_candidates(now_ist, times_str)
    upcoming = [t for t in todays if t > now_ist]
    if upcoming:
        return min(upcoming)
    # none left today -> first slot tomorrow
    tomorrow = now_ist + timedelta(days=1)
    return min(_today_candidates(tomorrow, times_str))


def seconds_until_entry_window(now: datetime = None, times_str: list[str] = None) -> float:
    """Positive = still waiting. <=0 and > -window = inside the entry window.
    Very negative = funding already passed, wait for the next cycle."""
    nft = next_funding_time(now, times_str)
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    entry_time = nft - timedelta(minutes=ENTRY_LEAD_MINUTES)
    return (entry_time - now_ist).total_seconds()


def is_in_entry_window(now: datetime = None, times_str: list[str] = None) -> bool:
    """True from ENTRY_LEAD_MINUTES before snapshot up to the snapshot itself."""
    nft = next_funding_time(now, times_str)
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    entry_time = nft - timedelta(minutes=ENTRY_LEAD_MINUTES)
    return entry_time <= now_ist < nft


def seconds_since_snapshot(now: datetime = None, times_str: list[str] = None) -> float:
    """Returns seconds elapsed since the MOST RECENT funding snapshot (negative
    if the next one hasn't happened yet). Used to trigger the close-immediately
    rule once POST_SNAPSHOT_CLOSE_DELAY_SEC has elapsed."""
    times_str = times_str or DELTA_FUNDING_TIMES_IST
    now_ist = (now or datetime.now(IST)).astimezone(IST)
    todays = _today_candidates(now_ist, times_str)
    past = [t for t in todays if t <= now_ist]
    if past:
        last = max(past)
    else:
        yesterday = now_ist - timedelta(days=1)
        last = max(_today_candidates(yesterday, times_str))
    return (now_ist - last).total_seconds()


def should_close_now(now: datetime = None, times_str: list[str] = None) -> bool:
    """Window is intentionally wider than POST_SNAPSHOT_CLOSE_DELAY_SEC alone:
    the main loop only ticks every MAIN_LOOP_INTERVAL_SEC (default 10s), and
    other work in that tick (basis-drift checks, repricing) can eat a few
    seconds, so we give ourselves several loop-ticks of margin rather than a
    single narrow window that a slow tick could miss entirely."""
    elapsed = seconds_since_snapshot(now, times_str)
    safety_margin_sec = 60
    return 0 <= elapsed <= POST_SNAPSHOT_CLOSE_DELAY_SEC + safety_margin_sec
