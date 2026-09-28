"""Keep `trash/` (gitignored scratch/removed-code holding area) out of collection.

Nothing under it is meant to run; without this, a `test_*.py` file left there
(e.g. by a removed feature) would still be picked up by pytest's default
whole-repo scan.
"""

collect_ignore_glob = ["trash/*"]
