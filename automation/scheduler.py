"""Scheduled jobs: the part that lets the agent act when nobody is watching.

Jobs live in the SQLite store, so they survive a redeploy - a schedule that
vanished on every restart would be useless. Three kinds, because "at a specific
time" means three different things to three different operators:

* ``at``    - once, at an absolute moment ("2026-10-12 07:00" or a unix stamp)
* ``every`` - on an interval ("90", "5m", "2h", "1d")
* ``cron``  - the familiar five fields ("30 7 * * 6" = Saturdays at 07:30)

Runs are strictly **serial**. The whole system shares one mouse and one
keyboard on one display, so two jobs at once would interleave their clicks and
corrupt both. A job that finds the desktop busy is not failed; it is retried
shortly, which is what an operator would have done by hand.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class SchedulerBusy(Exception):
    """The desktop is occupied; try this job again a little later."""


RETRY_AFTER_BUSY = 30.0
CRON_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
CRON_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")

_INTERVAL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_interval(text: Any) -> float:
    """``"90"``, ``90``, ``"5m"``, ``"2h"``, ``"1d"`` -> seconds."""
    raw = str(text or "").strip().lower()
    if not raw:
        raise ValueError("an interval is required")
    unit = raw[-1]
    if unit in _INTERVAL_UNITS:
        number, raw = raw[:-1], unit
    else:
        number, unit = raw, "s"
    try:
        seconds = float(number) * _INTERVAL_UNITS[unit]
    except ValueError:
        raise ValueError("cannot read %r as an interval" % text)
    if seconds < 1:
        raise ValueError("an interval must be at least one second")
    return seconds


def parse_moment(text: Any) -> float:
    """A unix timestamp, or a local ``YYYY-MM-DD[ HH:MM[:SS]]`` string."""
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("a moment is required")
    try:
        return float(raw)
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                    "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw, pattern).timestamp()
        except ValueError:
            continue
    raise ValueError("cannot read %r as a moment; use a unix timestamp or"
                     " YYYY-MM-DD HH:MM" % text)


def _parse_cron_field(text: str, low: int, high: int, name: str) -> Set[int]:
    values: Set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValueError("empty %s term" % name)
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError:
                raise ValueError("bad step in %s: %r" % (name, step_text))
            if step < 1:
                raise ValueError("step must be positive in %s" % name)
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            left, _, right = part.partition("-")
            try:
                start, end = int(left), int(right)
            except ValueError:
                raise ValueError("bad range in %s: %r" % (name, part))
        else:
            try:
                start = end = int(part)
            except ValueError:
                raise ValueError("bad value in %s: %r" % (name, part))
        if not low <= start <= end <= high:
            raise ValueError("%s value %d is outside %d-%d" % (name, start, low, high))
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError("%s matched nothing" % name)
    return values


def parse_cron(expression: Any) -> Tuple[Set[int], Set[int], Set[int], Set[int], Set[int]]:
    parts = str(expression or "").split()
    if len(parts) != 5:
        raise ValueError("a cron expression needs exactly 5 fields, got %r"
                         % expression)
    fields = []
    for text, (low, high), name in zip(parts, CRON_FIELD_RANGES, CRON_FIELD_NAMES):
        fields.append(_parse_cron_field(text, low, high, name))
    return tuple(fields)  # type: ignore[return-value]


def next_cron(expression: Any, after: float) -> float:
    """Next local time matching the expression, strictly after ``after``."""
    minutes, hours, doms, months, dows = parse_cron(expression)
    # Standard cron: when both day fields are restricted, either one matching is
    # enough. Getting this wrong silently skips or doubles every run.
    dom_restricted = len(doms) != 31
    dow_restricted = len(dows) != 7
    ordered_hours = sorted(hours)
    ordered_minutes = sorted(minutes)
    base = datetime.fromtimestamp(after)
    for offset in range(0, 368):
        day = (base + timedelta(days=offset)).date()
        if day.month not in months:
            continue
        dom_hit = day.day in doms
        dow_hit = (day.isoweekday() % 7) in dows
        if dom_restricted and dow_restricted:
            if not (dom_hit or dow_hit):
                continue
        elif not (dom_hit and dow_hit):
            continue
        for hour in ordered_hours:
            for minute in ordered_minutes:
                moment = datetime(day.year, day.month, day.day, hour, minute)
                if moment.timestamp() > after:
                    return moment.timestamp()
    raise ValueError("no time matches %r within a year" % expression)


def next_run(job: Dict[str, Any], now: float) -> Optional[float]:
    """When this job should next fire, or None when it never will."""
    kind = str(job.get("kind") or "")
    schedule = job.get("schedule")
    if kind == "at":
        moment = parse_moment(schedule)
        return moment if moment > now else None
    if kind == "every":
        return now + parse_interval(schedule)
    if kind == "cron":
        return next_cron(schedule, now)
    raise ValueError("unknown job kind: %r" % kind)


class Scheduler:
    """One background thread that fires due jobs, one at a time."""

    def __init__(self, store: Any, runner: Callable[[Dict[str, Any]], Dict[str, Any]],
                 clock: Any = time.time, sleep: Any = time.sleep,
                 interval: float = 5.0, on_log: Optional[Callable[[str, str], None]] = None) -> None:
        self.store = store
        self.runner = runner
        self._clock = clock
        self._sleep = sleep
        self.interval = interval
        self._on_log = on_log
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._run_lock = threading.Lock()
        self.running_job: Optional[str] = None
        self.last_tick: Optional[float] = None

    # -- lifecycle ------------------------------------------------------
    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.alive:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="agent-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    def _log(self, level: str, message: str) -> None:
        if self._on_log is not None:
            try:
                self._on_log(level, message)
            except Exception:  # noqa: BLE001 - logging must never break the loop
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - a bad job must not kill the loop
                self._log("warn", "scheduler tick failed: %s" % exc)
            self._stop.wait(self.interval)

    # -- the actual work ------------------------------------------------
    def ensure_schedules(self) -> List[str]:
        """Fill in a missing next_run_at, e.g. right after a restart."""
        fixed = []
        now = self._clock()
        for job in self.store.list_jobs(enabled_only=True):
            if job.get("next_run_at"):
                continue
            try:
                moment = next_run(job, now)
            except ValueError as exc:
                self.store.record_job_run(job["id"], "invalid", str(exc))
                self.store.set_job_next_run(job["id"], None)
                self._log("warn", "job %s has an unusable schedule: %s"
                          % (job.get("name"), exc))
                continue
            self.store.set_job_next_run(job["id"], moment)
            fixed.append(job["id"])
        return fixed

    def due_jobs(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = self._clock() if now is None else now
        due = []
        for job in self.store.list_jobs(enabled_only=True):
            moment = job.get("next_run_at")
            if not moment:
                continue
            if float(moment) <= now:
                due.append(job)
        # Oldest first, so a backlog drains in the order it was meant to.
        return sorted(due, key=lambda item: float(item["next_run_at"]))

    def tick(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = self._clock() if now is None else now
        self.last_tick = now
        results = []
        for job in self.due_jobs(now):
            # Serial on purpose: the desktop has one mouse and one keyboard.
            if not self._run_lock.acquire(blocking=False):
                results.append({"id": job["id"], "status": "deferred",
                                "reason": "another job is running"})
                break
            try:
                results.append(self._fire(job, now))
            finally:
                self._run_lock.release()
        return results

    def _fire(self, job: Dict[str, Any], now: float) -> Dict[str, Any]:
        job_id = job["id"]
        name = job.get("name") or job_id
        if self.store.kill_switch_engaged():
            self.store.record_job_run(job_id, "skipped", "kill switch is on",
                                      self._reschedule(job, now))
            self._log("warn", "job %s skipped: the kill switch is on" % name)
            return {"id": job_id, "status": "skipped", "reason": "kill switch"}
        self.running_job = job_id
        started = self._clock()
        try:
            outcome = self.runner(job) or {}
        except SchedulerBusy as exc:
            self.store.record_job_run(job_id, "deferred", str(exc),
                                      now + RETRY_AFTER_BUSY)
            self._log("info", "job %s deferred: %s" % (name, exc))
            return {"id": job_id, "status": "deferred", "reason": str(exc)}
        except Exception as exc:  # noqa: BLE001 - one bad job must not stop the rest
            self.store.record_job_run(job_id, "failed", str(exc),
                                      self._reschedule(job, now))
            self._log("warn", "job %s failed: %s" % (name, exc))
            return {"id": job_id, "status": "failed", "error": str(exc),
                    "elapsed": round(self._clock() - started, 2)}
        finally:
            self.running_job = None
        status = str(outcome.get("status") or "done")
        error = str(outcome.get("error") or "")
        if str(job.get("kind")) == "at":
            # A one-shot job is done; leaving it enabled would fire it forever.
            self.store.record_job_run(job_id, status, error, None)
            self.store.upsert_job({"enabled": False}, job_id=job_id, actor="scheduler")
        else:
            self.store.record_job_run(job_id, status, error,
                                      self._reschedule(job, now))
        self._log("info", "job %s finished: %s" % (name, status))
        return {"id": job_id, "status": status, "error": error,
                "elapsed": round(self._clock() - started, 2)}

    def _reschedule(self, job: Dict[str, Any], now: float) -> Optional[float]:
        try:
            return next_run(job, now)
        except ValueError:
            return None

    # -- validation used by the API -------------------------------------
    @staticmethod
    def validate(data: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
        """Check a job definition and report when it would next fire."""
        now = time.time() if now is None else now
        kind = str(data.get("kind") or "")
        if kind not in ("at", "every", "cron"):
            raise ValueError("job kind must be at, every or cron")
        probe = {"kind": kind, "schedule": data.get("schedule")}
        moment = next_run(probe, now)
        return {"kind": kind, "nextRunAt": moment,
                "nextRunText": (datetime.fromtimestamp(moment)
                                .strftime("%Y-%m-%d %H:%M:%S") if moment else
                                "never (that moment has passed)")}
