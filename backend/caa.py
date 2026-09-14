"""Cover Art Archive conventions — one literal for every path that mints a
phantom's cover (discography, mb_discovery, the seed / share exporters).
Kept apart from those modules on purpose: the exporters run as a CLI on the
launcher's interpreter, and importing the URL through discography dragged
in mb_backend and a database pool for one string."""

CAA_FRONT_URL = "https://coverartarchive.org/release-group/{rg}/front-500"
