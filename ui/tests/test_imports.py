def test_server_bootstrap_imports_harness():
    import server._paths  # noqa: F401
    from harness.loop import run_turn

    assert callable(run_turn)
