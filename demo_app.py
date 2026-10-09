"""Public demo entry point: sample data only, no HuskyCT, Outlook, or Google account.

Deploy this file (not app/dashboard.py) on Streamlit Community Cloud, or try it
locally with ``python -m streamlit run demo_app.py``. It keeps its own database
so a local demo never touches your real coursework.
"""

from __future__ import annotations

import os
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Must be set before app.config is first imported.
os.environ["HUSKYSYNC_DEMO"] = "1"
os.environ.setdefault("HUSKYSYNC_DATA_DIR", str(ROOT / ".husky_sync_demo"))

# Run the dashboard exactly as Streamlit would run it directly.
runpy.run_path(str(ROOT / "app" / "dashboard.py"), run_name="__main__")
