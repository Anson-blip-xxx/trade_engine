"""Controller-compatible ownership of already-created direct child workers.

This class creates no OS process and performs no DB, health or venue I/O.
Factories must either return their handle or fail before creating a process;
an exception after hiding a spawned child cannot be made safe by this adapter.
Member callbacks retain their own time bounds. In particular, broadcasting a
stop first does not make an old blocking ``stop`` implementation nonblocking.
"""

_START_FAILED = "WORKER_START_FAILED"
_GUARD_DENIED = "EXECUTION_GUARD_PERMISSION_DENIED"


def _sequence(values, *, allow_empty=False):
    if (
        type(values) not in (list, tuple)
        or not (0 if allow_empty else 1) <= len(values) <= 8
    ):
        raise ValueError("BOUNDED_WORKER_GROUP_REQUIRED")
    return tuple(values)


def _member_valid(member):
    return all(
        callable(getattr(member, name, None))
        for name in ("ready", "release", "alive", "stop")
    )


class WorkerGroup:
    """Keep every owned handle until every member explicitly confirms stop.

    Mutating lifecycle calls have one controller-thread owner. Member methods
    must not reenter this group; background member supervision stays internal
    to the member, not a competing caller of group release/stop.

    Use ``create`` when construction itself can partially fail. Direct
    construction accepts an already-owned list whose caller still retains all
    handles if validation fails. A group never changes binding, replaces an
    owned handle, restarts a member, or grants permission to execute trades.
    """

    def __init__(self, members, *, entries_enabled=False):
        members = _sequence(members)
        if len({id(member) for member in members}) != len(members) or not all(
            _member_valid(member) for member in members
        ):
            raise ValueError("EXPLICIT_WORKER_HANDLES_REQUIRED")
        self._initialize(members, entries_enabled)

    def _initialize(self, members, entries_enabled):
        if type(entries_enabled) is not bool:
            raise ValueError("EXPLICIT_WORKER_ENTRY_MODE_REQUIRED")
        self._members = list(members)
        self._confirmed = [None for _ in members]
        self._entries_enabled = entries_enabled
        self._startup_failure = None
        self._ready = self._released = self._closing = False

    @classmethod
    def create(cls, factories, *, entries_enabled=False):
        factories = _sequence(factories)
        if not all(callable(factory) for factory in factories):
            raise ValueError("EXPLICIT_WORKER_FACTORIES_REQUIRED")
        group = cls.__new__(cls)
        group._initialize((), entries_enabled)
        for factory in factories:
            try:
                member = factory()
                if any(member is existing for existing in group._members):
                    raise ValueError("DUPLICATE_WORKER_HANDLE")
                # Retain a returned object BEFORE validation. An invalid port
                # is unknown ownership, not permission to discard its handle.
                group._members.append(member)
                group._confirmed.append(None)
                if not _member_valid(member):
                    raise ValueError("EXPLICIT_WORKER_HANDLES_REQUIRED")
            except Exception:  # noqa: BLE001 - never expose factory args or lose earlier handles
                group._startup_failure = _START_FAILED
                group.request_stop()
                break
        return group

    @property
    def members(self):
        return tuple(self._members)

    @property
    def stop_confirmations(self):
        """True=confirmed, False=explicitly refused, None=not proved/exception."""
        return tuple(self._confirmed)

    @property
    def entries_enabled(self):
        return self._entries_enabled

    @property
    def startup_failure(self):
        return self._startup_failure

    def _failed_start(self, member):
        self._startup_failure = _START_FAILED
        try:
            if getattr(member, "startup_failure", None) == _GUARD_DENIED:
                self._startup_failure = _GUARD_DENIED
        except Exception:  # noqa: BLE001,S110 - only known fixed diagnostics can cross ports
            pass
        self.request_stop()

    def ready(self):
        if self._closing or self._startup_failure is not None or not self._members:
            return False
        if self._ready:
            return True
        for member in self._members:
            try:
                if member.ready() is True:
                    continue
            except Exception:  # noqa: BLE001,S110 - retain all handles on failed readiness
                pass
            self._failed_start(member)
            return False
        self._ready = True
        return True

    def release(self):
        if self._closing:
            raise RuntimeError("WORKER_GROUP_CLOSING")
        if not self._ready:
            raise RuntimeError("WORKER_GROUP_NOT_READY")
        if self._released:
            return
        for member in self._members:
            try:
                member.release()
            except Exception:  # noqa: BLE001 - released peers must be asked to stop immediately
                self._failed_start(member)
                raise RuntimeError("WORKER_GROUP_RELEASE_FAILED") from None
        self._released = True

    def alive(self):
        if self._closing or not self._members:
            return False
        for member in self._members:
            try:
                if member.alive() is True:
                    continue
            except Exception:  # noqa: BLE001,S110 - unknown member liveness is not success
                pass
            self.request_stop()
            return False
        return True

    def healthy(self):
        if self._closing or not self._members:
            return False
        for member in self._members:
            try:
                check = getattr(member, "healthy", None)
                if (member.alive() if check is None else check()) is True:
                    continue
            except Exception:  # noqa: BLE001,S110 - health uncertainty cannot become recovery
                pass
            return False
        return True

    def request_stop(self):
        self._closing = True
        for index, member in enumerate(self._members):
            if self._confirmed[index] is True:
                continue
            try:
                request = getattr(member, "request_stop", None)
                if callable(request):
                    request()
            except Exception:  # noqa: BLE001,S110 - one failed hint must not skip the others
                pass

    def stop(self):
        # All cooperative hints are broadcast before the first possibly
        # blocking stop. A member's request alone never confirms termination.
        self.request_stop()
        for index, member in enumerate(self._members):
            if self._confirmed[index] is True:
                continue
            try:
                stopped = member.stop()
                self._confirmed[index] = stopped if type(stopped) is bool else None
            except Exception:  # noqa: BLE001 - preserve unknown status and continue other stops
                self._confirmed[index] = None
        return all(confirmed is True for confirmed in self._confirmed)
