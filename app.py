"""
LoL Dodge Tracker — entry point.

Runs a pywebview window that serves the ui/ folder.
Python backend methods are callable from JS via window.pywebview.api.

Build as exe:
  pyinstaller --onefile --windowed --add-data "ui;ui" --name LoLAvoidTracker app.py
"""

import os
import sys
import webview
from backend import Tracker, ALL_PLATFORMS

tracker = Tracker()


def resource_path(relative: str) -> str:
    """Resolve a path that works both as a script and inside a PyInstaller exe."""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative)


class Api:
    """Methods exposed to the JavaScript frontend via window.pywebview.api.*"""

    def get_state(self):
        return tracker.get_state()

    def get_platforms(self):
        return ALL_PLATFORMS

    def set_config(self, api_key, platform):
        return tracker.set_config(api_key, platform)

    def add_player(self, riot_id, team_name=None):
        return tracker.add_player(riot_id, team_name or None)

    def remove_player(self, riot_id):
        return tracker.remove_player(riot_id)

    def rename_player(self, old_rid, new_rid):
        return tracker.rename_player(old_rid, new_rid)

    def move_player(self, riot_id, team_name, before_rid=None):
        return tracker.move_player(riot_id, team_name or None, before_rid or None)

    def set_player_enabled(self, riot_id, enabled):
        return tracker.set_player_enabled(riot_id, bool(enabled))

    def add_team(self, name):
        return tracker.add_team(name)

    def remove_team(self, name):
        return tracker.remove_team(name)

    def rename_team(self, old_name, new_name):
        return tracker.rename_team(old_name, new_name)

    def refresh_now(self):
        return tracker.refresh_now()

    def get_history(self):
        """Return the full persistent match-history log (history.json).

        Not surfaced in the UI yet — intended for future predictive
        features (day/hour play patterns, premade detection).
        """
        return tracker.get_history()


def on_shown():
    tracker.start_background_refresh()


if __name__ == "__main__":
    html_path = resource_path(os.path.join("ui", "index.html"))
    icon_path = resource_path("logo.ico")
    api = Api()

    window = webview.create_window(
        title="LoL Avoid Tracker",
        url=html_path,
        js_api=api,
        width=980,
        height=640,
        min_size=(700, 480),
        background_color="#15151D",
    )

    # `icon` is supported by webview.start on Windows and sets the
    # taskbar / window icon without needing the exe icon path.
    try:
        webview.start(on_shown, debug=False, icon=icon_path)
    except TypeError:
        # Older pywebview versions don't accept the icon kwarg — fall back.
        webview.start(on_shown, debug=False)
