import pytest

from ikarem import Ikarem
from ikarem.plugins import BasePlugin
from ikarem.testing import TestClient


class Logger(BasePlugin):
    name = "logger"
    priority = 50
    hits: list = []

    def register(self, app):
        @app.get("/x")
        async def x(req):
            return "x"

    async def on_request(self, req):
        self.hits.append(req.path)


class NeedsLogger(BasePlugin):
    name = "needs"
    requires = ["logger"]

    def register(self, app):
        pass


def test_plugin_register_and_hook():
    app = Ikarem()
    log = Logger()
    log.hits.clear()
    app.register(log)
    assert TestClient(app).get("/x").status_code == 200
    assert "/x" in log.hits


def test_plugin_dependency_order():
    app = Ikarem()
    app.register(Logger())
    app.register(NeedsLogger())
    names = [p.name for p in app.plugins.plugins]
    assert names.index("logger") < names.index("needs")


def test_plugin_missing_dep():
    app = Ikarem()
    with pytest.raises(ValueError):
        app.register(NeedsLogger())


def test_lifecycle():
    app = Ikarem()
    order = []

    @app.on_startup
    async def up():
        order.append("up")

    @app.on_shutdown
    async def down():
        order.append("down")

    c = TestClient(app)

    @app.get("/")
    async def h(req):
        return "ok"

    c.get("/")
    import asyncio

    asyncio.run(app.shutdown())
    assert "up" in order and "down" in order
