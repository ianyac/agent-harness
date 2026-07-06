# The root `uv run pytest` run is the harness lane's gate; ui tests joining
# it would couple the lanes' CI (a harness seam change would turn a harness
# PR red on ui code it cannot touch). Governance decision, not a default:
# the ui suite runs only when asked for explicitly:
#   UI_TESTS=1 uv run --group ui pytest ui/server/tests
import os

if not os.environ.get("UI_TESTS"):
    collect_ignore = ["server"]
