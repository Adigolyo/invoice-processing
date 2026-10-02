"""Task 1 smoke tests: the package is importable and exposes its version."""

import importlib


def test_intake_package_is_importable() -> None:
    module = importlib.import_module("intake")
    assert module is not None


def test_intake_exposes_version() -> None:
    import intake

    assert isinstance(intake.__version__, str)
    assert intake.__version__
