"""The Mac app's web view answers what a browser would.

A bare WKWebView drops each of these unless its UI delegate implements it: confirm()
returns false, window.open goes nowhere, and an <input type="file"> click (the chat
composer's Attach) does nothing at all. Pin every one so none silently goes missing.
"""
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "runner" / "menubar" / "Sources" / "main.swift"

DELEGATE_METHODS = (
    "runJavaScriptAlertPanelWithMessage",
    "runJavaScriptConfirmPanelWithMessage",
    "runJavaScriptTextInputPanelWithPrompt",
    "createWebViewWith",
    "runOpenPanelWith",
    "decidePolicyFor navigationAction",
)


def test_web_view_has_a_ui_delegate_for_every_browser_dialog():
    text = SOURCE.read_text()
    assert "web.uiDelegate = self" in text
    assert "web.navigationDelegate = self" in text
    missing = [m for m in DELEGATE_METHODS if m not in text]
    assert not missing, f"WKUIDelegate methods missing from the Mac app: {missing}"
