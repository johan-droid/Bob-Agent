"""Sample read-only tool plugin: text statistics.

Copy this folder to ``<tools_plugin_dir>/text_stats/`` to enable it.
Protocol: define ``handle(args: dict) -> dict``. Only the ``args`` dict is
passed in (no ToolContext crosses the plugin boundary); return a JSON-able
dict. ``execute``-risk plugins additionally run inside DockerSandbox via the
same ``handler.py:handle`` entry point — no code changes needed.
"""


def handle(args: dict) -> dict:
    text = str(args.get("text") or "")
    return {
        "chars": len(text),
        "words": len(text.split()),
        "lines": text.count("\n") + (1 if text else 0),
    }
