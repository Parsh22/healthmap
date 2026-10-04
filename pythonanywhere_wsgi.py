# Paste this into the WSGI configuration file linked from the PythonAnywhere "Web" tab.
import os
import sys

project_home = os.path.expanduser("~/health-map")
if project_home not in sys.path:
    sys.path.insert(0, project_home)

os.environ.setdefault("SESSION_COOKIE_SECURE", "1")
# Optional: require an invite code to sign up.
# os.environ["INVITE_CODE"] = "pick-something"

from app import app as application  # noqa: E402
