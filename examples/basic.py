"""Minimal working IKAREM app."""

from ikarem import Ikarem
from ikarem.db import DatabasePlugin

app = Ikarem(debug=True)
app.register(DatabasePlugin("sqlite:///:memory:"))


@app.on_startup
async def init_db():
    db = app.state_db
    await db.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, text TEXT)")


@app.get("/")
async def home(req):
    return {"framework": "ikarem"}


@app.get("/notes")
async def list_notes(req):
    return await req.app.state_db.fetch_all("SELECT * FROM notes")


@app.post("/notes")
async def add_note(req):
    body = await req.json()
    await req.app.state_db.execute("INSERT INTO notes (text) VALUES (?)", body.get("text", ""))
    return {"ok": True}, 201


if __name__ == "__main__":
    app.run(port=8000)
