"""
Built-in API keys for Sautium.

Last.fm keys are semi-public by design for desktop applications.
Many open-source scrobblers ship their keys in source code.
The API key provides read access (metadata, tags).
Scrobbling requires user authorization via OAuth (session key).

Genius is bring-your-own: a client access token is per-account, so none
ships here. Set GENIUS_ACCESS_TOKEN in .env to enable the plain-text lyrics
fallback; without it LRCLIB still serves synced lyrics, no key needed.
"""

# Last.fm — registered app "Sautium" (since 2026-10-05; Last.fm cannot rename
# an app, and the first one still bore the project's working name)
LASTFM_API_KEY = "6ee4acf6eb57e0e5eca1cf6bf6dde291"
LASTFM_API_SECRET = "c364e382b3bae77f5c80903df5129a74"
