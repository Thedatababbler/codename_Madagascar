"""The self-evolving loop's offline parts: ledger, bank, re-runs, memory replay, design cycle.

None of this runs inside a milestone's search (`control/fast_loop`); the
ledger is written when a search ends, everything else runs between tasks.
"""
