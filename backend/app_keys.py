"""
Built-in API keys for Sautium.

Last.fm keys are semi-public by design for desktop applications.
Many open-source scrobblers ship their keys in source code.
The API key provides read access (metadata, tags).
Scrobbling requires user authorization via OAuth (session key).
"""

# Last.fm — registered app "Sautium"
LASTFM_API_KEY = "45a94c0bb5961bc76f5724c325ef27ef"
LASTFM_API_SECRET = "896a4015a5fc5014740c4b5a461d726d"

# Genius — lyrics fallback (plain text only, LRCLIB preferred for synced lyrics)
GENIUS_ACCESS_TOKEN = "REDACTED-GENIUS-TOKEN"
