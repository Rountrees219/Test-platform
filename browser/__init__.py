"""
Ninja — Browser Automation Agent

Usage:
    # Via orchestrator (primary entry point)
    python -m browser                              # Default work loop
    python -m browser "Go to google.com and search for AI news"

    # Python API (for direct use in scripts)
    from browser.observer import observe
    from browser.actions import execute_action, set_elements
    from browser.browser_interface import BrowserInterface

    # Connect to persistent browser (preferred — tabs survive between tasks)
    browser = BrowserInterface.connect_cdp()
    obs = observe(browser, step=0)
    set_elements(obs["interactive_elements"])
    result = execute_action(browser, "click", {"selector": "#submit"})
    browser.stop()  # Disconnects only — browser keeps running

    # Browser server management
    from browser.browser_server import ensure_running
    ensure_running()  # Starts browser if not already running

    # Presets
    from browser.presets import get_preset_task
    task = get_preset_task("screenshot", url="https://example.com")
"""

from browser.config import NinjaConfig
from browser.presets import get_preset_task, list_presets

__all__ = ["NinjaConfig", "get_preset_task", "list_presets"]
