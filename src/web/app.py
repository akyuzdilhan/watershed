"""Watershed web console. First deployment: a single page that proves the hosting works."""

from dash import Dash, html

app = Dash(__name__, title="Watershed")
server = app.server

app.layout = html.Main(
    [
        html.H1("Watershed"),
        html.P("Football's fine margins, measured live and explained to you."),
        html.P("Work in progress. Synthetic data only."),
    ],
    style={"fontFamily": "system-ui, sans-serif", "maxWidth": "720px", "margin": "48px auto", "padding": "0 16px"},
)

if __name__ == "__main__":
    app.run(debug=True)
