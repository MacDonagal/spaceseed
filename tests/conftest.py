import os
import tempfile

_data_dir = tempfile.mkdtemp(prefix="spaceseed-tests-")
os.environ.update(
    DATABASE_URL=f"sqlite:///{_data_dir}/test.db",
    DATA_DIR=_data_dir,
    TORZNAB_URL="http://indexer.test/api",
    TORZNAB_API_KEY="secret-key",
    QBITTORRENT_URL="http://qbittorrent.test:8080",
    SPACESEED_PASSWORD="",
    C411_API_KEY="",
    TELEGRAM_BOT_TOKEN="",
    TELEGRAM_CHAT_ID="",
)

import pytest

from app.database import Base, engine


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
