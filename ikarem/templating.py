"""Templates: Flask-style render_template DX, Jinja2-backed, optional dep.

``pip install ikarem[jinja]`` — the core stays stdlib-only, so Jinja2 loads
lazily with an error message that tells you the exact install command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class Templates:
    def __init__(self, directory: str | Path):
        self.directory = str(directory)
        self._env: Any = None

    def _env_for(self) -> Any:
        if self._env is None:
            try:
                import jinja2
            except ImportError as e:
                raise RuntimeError("pip install ikarem[jinja] (needs jinja2)") from e
            self._env = jinja2.Environment(
                loader=jinja2.FileSystemLoader(self.directory),
                autoescape=True,
            )
        return self._env

    def render(self, template_name: str, **ctx: Any) -> str:
        # First arg is template_name (not `name`): template context dicts
        # legitimately contain a `name` key, which would collide.
        return self._env_for().get_template(template_name).render(**ctx)

    def response(
        self, template_name: str, status_code: int = 200, headers: dict[str, str] | None = None, **ctx: Any
    ) -> Any:
        from .http import HTMLResponse

        return HTMLResponse(self.render(template_name, **ctx), status_code=status_code, headers=headers)
