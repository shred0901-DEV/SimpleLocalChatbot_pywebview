"""Lightweight, thread-safe signal implementation to replace Qt Signals."""
import logging
import threading


class Signal:
    """Simple callback dispatcher mimicking Qt Signal."""

    def __init__(self, *arg_types):
        self._arg_types = arg_types
        self._slots = []
        self._lock = threading.Lock()

    def connect(self, slot):
        with self._lock:
            if slot not in self._slots:
                self._slots.append(slot)

    def disconnect(self, slot):
        with self._lock:
            if slot in self._slots:
                self._slots.remove(slot)

    def emit(self, *args, **kwargs):
        with self._lock:
            slots = list(self._slots)
        for slot in slots:
            try:
                slot(*args, **kwargs)
            except Exception:
                logging.exception("Exception in signal handler %r", slot)
