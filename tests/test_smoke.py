from document_translator import __version__


def test_package_smoke() -> None:
    assert __version__ == "0.1.0"
