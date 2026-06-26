"""Sautium streaming-preview subsystem.

Pluggable providers (``base.StreamProvider``) turn a phantom track into
HQPlayer-playable audio; an in-memory local proxy (``proxy``) serves it to
HQPlayer's native playlist alongside owned ``file://`` tracks. YouTube ships in
core; DRM providers are external BYO modules. See ``base.py`` for the versioned
contract."""
