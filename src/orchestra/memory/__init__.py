"""Memory banks for the first pass, the repair and the test author (memory spec 2026-10-09).

Three isolated banks live under ``memory/`` at the repository root, never in a
workspace. Each is a skill document for its judge plus dictionary files. A run
pins one read-only snapshot (``memory/versions/v<N>``) and injects recalled
entries through the program only: judge -> recall -> assemble -> delivery check.
Everything here is off unless ``memory.enabled`` is set; with it off no code
path changes.
"""
