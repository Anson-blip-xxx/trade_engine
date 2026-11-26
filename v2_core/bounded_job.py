"""Nonblocking supervision of one disposable, directly owned spawn child.

The owner must call ``poll`` regularly: no background thread drives deadlines.
Targets may not spawn descendants or receive pre-opened database connections.
They must create their own resources and retain all ordinary account/DB locks.
Process death is not proof of a rollback or of immediate backend lock release;
every started job requires reconciliation, regardless of its reported outcome.
This module does not grant execution authority or wire an existing controller.
"""

import math
import multiprocessing
import time
from dataclasses import dataclass
from uuid import uuid4

_RESULTS = {b"COMPLETED", b"BLOCKED", b"BUSY", b"FAILED"}


class _StopFlag:
    """One parent writer, child readers; no cross-process lock to strand.

    The single shared byte is a cooperative hint, never an execution fence or
    a stop acknowledgement. Only process death and reaping confirm a stop.
    ``wait`` polls in the child; the parent only performs a byte assignment.
    """

    def __init__(self, value):
        self._value = value

    def set(self):
        self._value.value = 1

    def is_set(self):
        return self._value.value == 1

    def wait(self, timeout=None):
        if timeout is not None and (
            type(timeout) not in (int, float)
            or not 0 <= timeout <= 2**53
            or not math.isfinite(timeout)
        ):
            raise ValueError("BOUNDED_JOB_STOP_WAIT_INVALID")
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            if deadline is None:
                time.sleep(0.02)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self.is_set()
                time.sleep(min(0.02, remaining))
        return True


def cooperative_flag(context):
    """Create a lock-free one-writer hint; never a permission or stop proof."""
    return _StopFlag(context.RawValue("b", 0))


@dataclass(frozen=True)
class BoundedJobPolicy:
    work_seconds: float
    stop_seconds: float
    terminate_seconds: float
    kill_seconds: float

    def __post_init__(self):
        values = (
            self.work_seconds,
            self.stop_seconds,
            self.terminate_seconds,
            self.kill_seconds,
        )
        if (
            any(
                type(value) not in (int, float)
                or not 0 < value <= 3600
                or not math.isfinite(value)
                for value in values
            )
            or sum(values) > 3600
        ):
            raise ValueError("BOUNDED_JOB_POLICY_REQUIRED")


def _run(target, stop, args, channel):
    """Only fixed statuses cross IPC; exception text and arguments never do."""
    status = b"FAILED"
    try:
        result = target(stop, *args)
        if type(result) is str and result in {"COMPLETED", "BLOCKED", "BUSY"}:
            status = result.encode("ascii")
    except BaseException:  # noqa: BLE001,S110 - child errors are fixed protocol outcomes
        pass
    try:
        channel.send_bytes(status)
    except (EOFError, OSError):
        pass
    finally:
        channel.close()


class BoundedProcessJob:
    """Single-owner API; poll/stop never wait for a process or an IPC frame.

    Deadlines are absolute, including stop escalation. A late poll never starts
    a fresh grace period. If death/reaping cannot be confirmed, the only handle
    remains owned here and ``start`` refuses to replace it. Injected contexts
    are test ports and must obey the spawn Process/RawValue/Pipe contracts.
    """

    def __init__(self, target, *, policy, clock=time.monotonic, context=None):
        if (
            not callable(target)
            or type(policy) is not BoundedJobPolicy
            or not callable(clock)
        ):
            raise ValueError("EXPLICIT_BOUNDED_JOB_REQUIRED")
        self.target, self.policy, self.clock = target, policy, clock
        self.context = (
            context if context is not None else multiprocessing.get_context("spawn")
        )
        method = getattr(self.context, "get_start_method", None)
        if method is not None and method() != "spawn":
            raise ValueError("BOUNDED_JOB_SPAWN_REQUIRED")
        self._process = self._channel = self._stop_event = None
        self._job_id = None
        self._status = "IDLE"
        self._cause = self._phase = None
        self._deadline = None
        self._last_now = None

    def _now(self):
        now = self.clock()
        if (
            type(now) not in (int, float)
            or not 0 <= now <= 2**53
            or not math.isfinite(now)
            or self._last_now is not None
            and now < self._last_now
        ):
            raise ValueError("BOUNDED_JOB_CLOCK_INVALID")
        self._last_now = now
        return now

    def _result(self):
        return {
            "job_id": self._job_id,
            "status": self._status,
            "stop_confirmed": self._process is None,
            "execution_authorized": False,
            "outcome_requires_reconciliation": self._job_id is not None,
        }

    @staticmethod
    def _close(resource):
        if resource is not None:
            try:
                resource.close()
            except Exception:  # noqa: BLE001,S110 - diagnostics must not expose resource data
                pass

    def start(self, *args):
        if self._process is not None:
            self.poll()
        if self._process is not None:
            raise RuntimeError("BOUNDED_JOB_NOT_STOPPED")
        now = self._now()
        self._job_id = str(uuid4())
        self._status, self._cause, self._phase = "RUNNING", None, "WORK"
        self._deadline = now + self.policy.work_seconds
        child = None
        try:
            # Event.set acquires a process-shared Condition lock. A child
            # killed while owning it could deadlock this supervisor. The stop
            # hint therefore uses a raw byte, not a semaphore-backed Event.
            self._stop_event = cooperative_flag(self.context)
            self._channel, child = self.context.Pipe(duplex=False)
            self._process = self.context.Process(
                target=_run, args=(self.target, self._stop_event, args, child)
            )
            self._process.start()
        except Exception:  # noqa: BLE001 - failed spawn may have partially started a child
            if self._process is None or self._process.pid is None:
                self._close(self._process)
                self._process = None
                self._close(self._channel)
                self._channel = None
                self._status = "FAILED"
            else:
                self._begin_stop("FAILED", now)
        finally:
            # The parent must never keep the child's write endpoint alive.
            self._close(child)
        return self._result()

    def _begin_stop(self, cause, start):
        if self._cause is not None:
            return
        self._cause, self._phase, self._status = cause, "COOPERATIVE", "STOPPING"
        self._deadline = start + self.policy.stop_seconds
        try:
            self._stop_event.set()
        except Exception:  # noqa: BLE001,S110 - escalation still runs when cooperation fails
            pass

    def _reap(self):
        process = self._process
        try:
            process.join(timeout=0)
            if process.is_alive() or process.exitcode is None:
                return False
        except Exception:  # noqa: BLE001 - never abandon an unconfirmed process handle
            return False
        status = self._cause or "FAILED"
        if self._cause is None and process.exitcode == 0:
            try:
                # Read only after direct-child death: no live writer can leave
                # a partial frame blocking the supervisor. Descendants are
                # forbidden by this API's target contract.
                if self._channel.poll(0):
                    payload = self._channel.recv_bytes(maxlength=64)
                    if payload in _RESULTS:
                        status = payload.decode("ascii")
            except (EOFError, OSError, ValueError):
                pass
        self._close(self._channel)
        self._close(process)
        self._channel = self._process = self._stop_event = None
        self._status = status
        return True

    def poll(self):
        if self._process is None:
            return self._result()
        now = self._now()
        if self._cause is None and now >= self._deadline:
            # Even an unobserved normal return cannot prove timely completion.
            self._begin_stop("TIMEOUT", self._deadline)
        if self._reap():
            return self._result()
        if self._cause is None:
            return self._result()
        while now >= self._deadline and self._phase != "UNCONFIRMED":
            if self._phase == "COOPERATIVE":
                action, next_phase, duration = (
                    self._process.terminate,
                    "TERMINATE",
                    self.policy.terminate_seconds,
                )
            elif self._phase == "TERMINATE":
                action, next_phase, duration = (
                    self._process.kill,
                    "KILL",
                    self.policy.kill_seconds,
                )
            else:
                self._phase, self._status = "UNCONFIRMED", "STOP_UNCONFIRMED"
                break
            try:
                action()
            except Exception:  # noqa: BLE001,S110 - keep ownership and bounded escalation
                pass
            self._phase = next_phase
            self._deadline += duration
            if self._reap():
                break
        return self._result()

    def stop(self):
        result = self.poll()
        if self._process is None:
            return result
        self._begin_stop("STOPPED", self._now())
        return self.poll()
