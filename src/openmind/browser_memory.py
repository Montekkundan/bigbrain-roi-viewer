"""Local browser diagnostics, kept in the browser rather than synced to Python."""

from pathlib import Path

serve = {"openmind-assets": str(Path(__file__).with_name("static"))}
scripts = ["openmind-assets/browser-memory.js"]
vue_use = ["openmindMemory"]


def widget(**kwargs):
    from trame_client.widgets.core import AbstractElement

    element = AbstractElement("openmind-memory", **kwargs)
    element._event_names.append(("sample_memory", "sampleMemory"))
    return element
