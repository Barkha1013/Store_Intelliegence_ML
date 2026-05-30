import sys
import os

# Ensure project root is on sys.path for all tests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# anyio pytest backend
import pytest

def pytest_configure(config):
    config.addinivalue_line("markers", "anyio: mark test as async (anyio backend)")
