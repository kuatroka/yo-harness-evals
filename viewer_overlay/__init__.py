"""Launch and rerun for the Harbor viewer, as a repo-owned overlay.

Harbor's viewer is a compiled SPA over its own Python API, so every
customization used to be a patch against site-packages. Here the overlay adds
routes and static files on top of the app `harbor.viewer.server.create_app`
returns, and leaves the installed package untouched. A Harbor upgrade rebuilds
`.viewer_static/` from the new bundle on the next start.
"""
