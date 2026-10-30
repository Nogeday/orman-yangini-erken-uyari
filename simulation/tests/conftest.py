import sys
from pathlib import Path

import pytest

SIM_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = SIM_DIR.parent
if str(SIM_DIR) not in sys.path:
    sys.path.insert(0, str(SIM_DIR))

RECORDING = REPO_DIR / "docs" / "ornek_seri_kayit.tsv"


@pytest.fixture
def app_config():
    from server.config import Settings, load_app_config

    return load_app_config(Settings(session_log=False))


@pytest.fixture
def engine_factory(app_config):
    """Sahte saatli motor + yayınlanan olayların listesi."""
    from server.state import Engine, FakeClock

    def make(start: float = 0.0):
        clock = FakeClock(start=start)
        events: list[dict] = []
        engine = Engine(app_config, clock=clock, emit=events.append)
        return engine, clock, events

    return make


@pytest.fixture(autouse=True)
def _telegram_kapali(monkeypatch):
    # Silmek yetmez: .env yeniden yüklenir. Boş değer ise ezilmez, Telegram kapalı kalır.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
