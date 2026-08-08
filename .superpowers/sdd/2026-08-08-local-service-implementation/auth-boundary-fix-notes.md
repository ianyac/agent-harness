# C2 / I1 / I2 / I7 auth-boundary fix evidence

## RED

- Command: `cd ui && uv run pytest tests/test_auth_boundary.py -q`
- Result before production edits: `16 failed, 1 passed in 0.30s`.
- Deterministic observed gaps:
  - bootstrap redirected to `/` and still enabled the credential cookie;
  - anonymous/mismatched-origin malformed bodies reached JSON/UTF-8 parsing (`400`) before auth (`401`/`403`);
  - arbitrary methods with `static_root=None` reached framework routing (`404`) before auth;
  - Tauri preflight reached framework method handling (`405`) with no CORS response;
  - browser static/API capability, launch-secret retirement, and unsafe static-path regressions failed at the missing capability redirect contract.

## GREEN

- New regression command: `cd ui && uv run pytest tests/test_auth_boundary.py -q`
- Result: `17 passed in 0.24s`.
- Warning-clean focused command:
  `cd ui && uv run pytest -W error tests/test_auth.py tests/test_auth_boundary.py tests/test_app_rest.py tests/test_app_ws.py tests/test_cli.py -q`
- Result: `265 passed in 7.32s`.
- Warning-clean full UI command: `cd ui && uv run pytest -W error -q`
- Result: `379 passed in 7.87s`.
- The focused gate covers browser path/fragment capability exchange, launch
  secret retirement, cookie rejection, bearer and WebSocket authentication,
  static-path scoping, pre-routing malformed-body rejection with and without a
  static root, arbitrary methods, exact Tauri CORS/preflight, process-level
  browser startup, and unsafe static-path rejection before SPA fallback.
