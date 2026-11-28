import logging

import pytest

from backend.main import desktop_access_errors


@pytest.mark.parametrize("status, retained", [(200, False), (304, False), (404, True), (503, True)])
def test_desktop_access_errors(status, retained):
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s %s %s %s %d",
                               ("client", "GET", "/status?query=private", "1.1", status), None)
    assert desktop_access_errors(record) is retained
    if retained:
        assert record.getMessage() == f"client GET /status 1.1 {status}"
