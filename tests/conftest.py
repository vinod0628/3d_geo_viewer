from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[1] / "demo_data"


@pytest.fixture(scope="session")
def demo() -> Path:
    if not DEMO.exists():
        pytest.skip("demo_data folder not present")
    return DEMO


@pytest.fixture(scope="session")
def surpac_sqlite() -> Path:
    """The demo drillholes (94 holes) as a plain SQLite drillhole database: Surpac's tables collar, survey,
    geology, sample, weathering, translation, styles (dip negative downward), read like a Surpac database."""
    return Path(__file__).resolve().parent / "data" / "demo_drillholes.sqlite"


@pytest.fixture(autouse=True)
def _default_database():
    """Every test starts on SQLite with no workspace folder (the app's default is MySQL, which
    tests use only when asked): the setting is app-wide state that a test may change."""
    from geoviewer.io import database

    database.configure(database.DatabaseConfig(backend="sqlite"))
    database.set_project_folder(None)
    yield
    database.configure(database.DatabaseConfig(backend="sqlite"))
    database.set_project_folder(None)


@pytest.fixture(scope="session")
def _test_cache_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("geoviewer_cache")


@pytest.fixture(autouse=True)
def _private_cache(_test_cache_dir, monkeypatch):
    r"""Block model caches made by the tests go to a temporary folder, never into the user's
    %LOCALAPPDATA%\GeoViewer\cache (every test copies the demo files to a new folder, and each
    new path would leave another cache file there)."""
    from geoviewer.io import cache

    monkeypatch.setattr(cache, "default_cache_dir", lambda: _test_cache_dir)
    yield


@pytest.fixture(autouse=True)
def _license_in_tests(monkeypatch):
    """The feature gates and the startup check ask ``licensing.current()`` / ``refresh()``.
    In the tests, pretend this PC has a full, valid license so GUI tests reach the features.
    This injection is in-process only (it is not in the shipped app)."""
    from datetime import date

    from geoviewer import licensing

    lic = licensing.License(name="Test", issued=date(2026, 1, 1), expires=None,
                            edition=2, features=licensing.FEAT_DATABASE | licensing.FEAT_EXPORT)
    ok = licensing.Status(True, lic, "", None)
    monkeypatch.setattr(licensing, "current", lambda: ok)
    monkeypatch.setattr(licensing, "refresh", lambda: ok)
    yield


@pytest.fixture(autouse=True)
def _english():
    """Tests read the English texts: whatever language this PC's GeoViewer is set to."""
    from geoviewer import i18n

    i18n.set_language("en")
    yield
    i18n.set_language("en")
