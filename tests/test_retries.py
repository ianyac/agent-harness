import httpx
import pytest

from harness.llm import RetryableHTTPError, with_retries


def flaky(failures: list[Exception], result: str = "ok"):
    """An attempt that raises the scripted failures, then succeeds."""

    def attempt():
        if failures:
            raise failures.pop(0)
        return result

    return attempt


def test_transient_transport_errors_are_retried_until_success():
    sleeps = []
    attempt = flaky([httpx.ConnectError("boom"), httpx.ReadError("boom")])
    assert with_retries(attempt, sleep=sleeps.append) == "ok"
    assert len(sleeps) == 2
    assert sleeps[0] < sleeps[1]  # backoff grows


def test_retryable_http_statuses_are_retried():
    sleeps = []
    attempt = flaky([RetryableHTTPError(503), RetryableHTTPError(429)])
    assert with_retries(attempt, sleep=sleeps.append) == "ok"
    assert len(sleeps) == 2


def test_retry_after_header_is_honored_as_a_floor():
    sleeps = []
    attempt = flaky([RetryableHTTPError(429, retry_after=7.0)])
    with_retries(attempt, sleep=sleeps.append)
    assert sleeps[0] >= 7.0


def test_exhausted_retries_reraise_the_last_error():
    errors = [httpx.ConnectError("boom")] * 10
    with pytest.raises(httpx.ConnectError):
        with_retries(flaky(errors), max_retries=3, sleep=lambda _: None)
    assert len(errors) == 10 - 4  # initial try + 3 retries consumed


def test_non_retryable_errors_raise_immediately():
    sleeps = []
    attempt = flaky([RuntimeError("codex HTTP 401: bad auth")])
    with pytest.raises(RuntimeError):
        with_retries(attempt, sleep=sleeps.append)
    assert sleeps == []
