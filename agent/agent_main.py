"""Agent wrapper (issue #15): the entry program the console installs.

Co-located with brick_telemetry.py because pybricksdev's multi-file
compile resolves imports from this directory (docs-review finding 14) —
the library is bundled into the downloaded image.
"""

import brick_telemetry

brick_telemetry.run()
