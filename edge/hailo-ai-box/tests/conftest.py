import importlib.util

import pytest

from aibox.config import Settings
from aibox.fakes import make_fake_services


def pytest_collection_modifyitems(config, items):
    if importlib.util.find_spec("hailo_platform") is None:
        skip = pytest.mark.skip(reason="hailo_platform not installed (laptop)")
        for item in items:
            if "pi" in item.keywords:
                item.add_marker(skip)


@pytest.fixture
def settings():
    return Settings(fake=True)


@pytest.fixture
def services(settings):
    return make_fake_services(settings)
