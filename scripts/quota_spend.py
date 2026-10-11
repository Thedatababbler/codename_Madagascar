#!/usr/bin/env python3
"""Spend accounting for capped paid runs, robust to a quota-window reset (2026-10-11).

The account's window rolled over in the middle of A4 (47% -> 1%), which made
``current - start`` negative and disabled the cap. ``Spend`` treats a reading below
the previous one as a reset: the spend so far is banked and the baseline restarts.
"""

from __future__ import annotations


class Spend:
    """Quota accounting that survives a window reset: a reading below the last one means the window
    rolled over; the spend so far is banked and the baseline restarts at the new reading."""

    def __init__(self, spent_before: float = 0.0):
        from joint_experiment import read_window

        self._read = read_window
        w = read_window()
        if w is None:
            raise SystemExit("cannot read the quota window; refusing to spend")
        self.w0 = self.last = w
        self.banked = spent_before
        self.resets: list[dict] = []

    def poll(self) -> float:
        w = self._read()
        if w is not None:
            if w < self.last:
                self.banked += self.last - self.w0
                self.resets.append({"from": self.last, "to": w, "banked": round(self.banked, 2)})
                self.w0 = w
            self.last = w
        return self.used()

    def used(self) -> float:
        return round(self.banked + self.last - self.w0, 2)


__all__ = ["Spend"]
