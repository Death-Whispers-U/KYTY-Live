"""When to take automatic pictures of the game window during a run, and which ones to keep.

Periodic snapshots show what the screen looked like before a run went wrong; only the newest
`keep` of them are kept. A run-ending event (a fatal error, a host exception, a lost GPU
device) also takes one at once. Event and manual snapshots are never deleted.
"""

TRIGGER_KINDS = frozenset({"fatal", "host_exception", "device_lost", "vulkan_failure"})


def snapshot_name(t, reason=""):
    return f"snap-{t:07.1f}s" + (f"-{reason}" if reason else "") + ".png"


class AutoSnapshots:
    def __init__(self, every, keep, max_events=5):
        if every != 0 and every < 1:
            raise ValueError("auto snapshots must be off (0) or at least 1 second apart")
        if keep < 1:
            raise ValueError("keep at least one auto snapshot")
        self.every, self.keep, self.max_events = every, keep, max_events
        self._next = every
        self._periodic = []
        self._events = 0

    def due(self, t):
        return self.every > 0 and t >= self._next

    def taken(self, t, name):
        """Records a periodic snapshot; returns the older ones to delete."""
        self._next = t + self.every
        self._periodic.append(name)
        drop, self._periodic = self._periodic[:-self.keep], self._periodic[-self.keep:]
        return drop

    def claim_event(self):
        if self._events >= self.max_events:
            return False
        self._events += 1
        return True

    @staticmethod
    def triggers(issue):
        return issue.get("kind") in TRIGGER_KINDS
