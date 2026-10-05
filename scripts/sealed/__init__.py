"""Sealed readers of the held-out suite and the dataset reference (author-evolution spec §5).

Only modules in this directory may open the held-out directory or the
reference implementation. Every script here emits case ids, labels, public
symbol names and numbers -- never source, assertion text or literals -- and
``_guard.assert_no_source_overlap`` checks that before anything is written.
"""
