"""Watershed web app: the console at `/` (read only for a visitor in public mode, every control for a signed-in producer), the public overlay at `/overlay`.

Run locally: `DESK_DIR=data/desk PUBLIC_MODE=0 python -m web.app` (from `src/`, or with `src` on the path). In the container, gunicorn serves `web.app:server`.
"""

import os

from dash import Dash, Input, Output, dcc, html

from web import console, overlay, security

app = Dash(__name__, title="Watershed", update_title=None, suppress_callback_exceptions=True)
server = app.server
security.install(server)

app.layout = html.Div([dcc.Location(id="url", refresh=False), html.Div(id="page")])


def route(pathname: str | None):
    path = (pathname or "/").rstrip("/") or "/"
    if path == "/overlay":
        return overlay.layout()
    if path == "/":
        return console.layout(security.producer())
    return html.Div([html.H1("Not found"), html.A("Overlay", href="/overlay")], className="sign-in")


app.callback(Output("page", "children"), Input("url", "pathname"))(route)
console.register(app)
overlay.register(app)

if __name__ == "__main__":
    os.environ.setdefault("PUBLIC_MODE", "0")
    # load_dotenv off: Flask would otherwise pick up a parent .env with COSMOS_ENDPOINT and read the cloud desk
    app.run(debug=os.environ.get("DASH_DEBUG", "0") == "1", port=int(os.environ.get("PORT", "8050")), load_dotenv=False)
