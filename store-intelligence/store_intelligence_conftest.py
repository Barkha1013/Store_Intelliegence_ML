"""
Shared conftest helper imported by test modules to configure the DB path
before any app modules are imported.

Import this at the top of each test file:
    import store_intelligence_conftest  # noqa: F401
"""
import os
import tempfile

# Only set a default tmp path if DB_PATH is not already set by the test fixture.
# Individual tests use the tmp_db fixture to override this per-test.
if "DB_PATH" not in os.environ:
    _tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    os.environ["DB_PATH"] = _tmp.name
    _tmp.close()
