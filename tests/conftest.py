"""Load the plugin directory as package ``hermes_showreel`` (the same way Hermes imports it)."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    if "hermes_showreel" in sys.modules:
        return sys.modules["hermes_showreel"]
    spec = importlib.util.spec_from_file_location("hermes_showreel", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hermes_showreel"] = mod
    spec.loader.exec_module(mod)
    return mod


pkg = _load()


def fake_key() -> str:
    from hermes_showreel.sample import _fake_key
    return _fake_key()


class FakeCtx:
    """Minimal stand-in for PluginContext used by unit tests (real discovery is in test_hermes_load)."""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.hooks, self.tools, self.commands, self.cli = {}, {}, {}, {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, cb, **kw):
        self.hooks[name] = cb

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = handler

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli[name] = setup_fn

    def current_cron_execution(self):
        return None

    def on_unload(self, cb):
        pass


@pytest.fixture
def store(tmp_path):
    from hermes_showreel.capture import Store
    s = Store(tmp_path / "data.db")
    yield s
    s.close()


@pytest.fixture
def app(tmp_path, store):
    from hermes_showreel import Showreel
    ctx = FakeCtx({"output_dir": str(tmp_path / "renders"), "narration": "off"})
    return Showreel(ctx, store=store)
