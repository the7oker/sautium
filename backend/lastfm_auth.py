#!/usr/bin/env python3
"""
One-time Last.fm authentication to obtain a session key for scrobbling.

Usage:
    python lastfm_auth.py

The script will:
1. Generate an auth URL
2. Ask you to open it in a browser and authorize
3. Exchange the token for a permanent session key
4. Print the session key to add to .env
"""

import os
import sys

import pylast

API_KEY = os.getenv("LASTFM_API_KEY")
API_SECRET = os.getenv("LASTFM_API_SECRET")

if not API_KEY or not API_SECRET:
    print("ERROR: LASTFM_API_KEY and LASTFM_API_SECRET must be set")
    sys.exit(1)

# Create network with password hash auth
skg = pylast.SessionKeyGenerator(
    pylast.LastFMNetwork(api_key=API_KEY, api_secret=API_SECRET)
)

# Get auth URL
url = skg.get_web_auth_url()

print(f"\n{'='*60}")
print("Last.fm Scrobbling Authorization")
print(f"{'='*60}")
print(f"\n1. Open this URL in your browser:\n")
print(f"   {url}")
print(f"\n2. Click 'Yes, allow access'")
print(f"\n3. Press Enter here after authorizing...")

input()

try:
    session_key = skg.get_web_auth_session_key(url)
    print(f"\nSession key obtained!")
    print(f"\nAdd this to your .env file:")
    print(f"\n   LASTFM_SESSION_KEY={session_key}")
    print(f"\nThen restart the playback-tracker container.")
    print(f"{'='*60}\n")
except Exception as e:
    print(f"\nERROR: {e}")
    print("Make sure you authorized the app in the browser first.")
    sys.exit(1)
