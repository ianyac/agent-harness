# Root pytest scope request

Please add the following configuration to the shared root `pyproject.toml`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

The UI service owns an independent `ui/tests` suite with different dependencies.
Without scoped root collection, root pytest imports that UI project even though the
root environment does not install its dependencies. The UI lane does not edit the
shared root `pyproject.toml`.
