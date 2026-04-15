"""UI constants and styles."""

from questionary import Style

custom_style = Style([
    ('qmark', 'fg:#00ff00 bold'),
    ('question', 'bold'),
    ('pointer', 'fg:#00ff00 bold'),
    ('selected', 'fg:#00ff00'),
    ('separator', 'fg:#555555'),
])

SERVICE_URL = "http://localhost:5002"
