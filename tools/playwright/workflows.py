"""
Predefined browser verification workflows.

Tasks can set metadata.workflow to one of these names instead of raw steps.
"""

from __future__ import annotations

from typing import Any


def build_workflow(name: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Return step list for a named workflow.

    Params vary by workflow (url, selectors, credentials, etc.).
    """
    builders = {
        "verify_login": _verify_login,
        "verify_button": _verify_button,
        "verify_notification_badge": _verify_notification_badge,
        "verify_mobile_layout": _verify_mobile_layout,
    }
    fn = builders.get(name)
    if not fn:
        raise ValueError(f"Unknown browser workflow: {name}")
    return fn(params)


def _verify_login(p: dict[str, Any]) -> list[dict[str, Any]]:
    url = p["url"]
    return [
        {"action": "goto", "url": url},
        {"action": "fill", "selector": p.get("email_selector", "#email"), "value": p["email"]},
        {"action": "fill", "selector": p.get("password_selector", "#password"), "value": p["password"]},
        {"action": "click", "selector": p.get("submit_selector", 'button[type="submit"]')},
        {"action": "wait_for", "selector": p.get("success_selector", ".dashboard"), "timeout_ms": p.get("timeout_ms", 15000)},
        {"action": "assert_visible", "selector": p.get("success_selector", ".dashboard")},
        {"action": "screenshot", "label": "login-success"},
    ]


def _verify_button(p: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [{"action": "goto", "url": p["url"]}]
    if p.get("before_click_selector"):
        steps.append({"action": "assert_visible", "selector": p["before_click_selector"]})
    steps.extend([
        {"action": "click", "selector": p["button_selector"]},
        {"action": "wait_for", "selector": p.get("result_selector", p["button_selector"]), "timeout_ms": p.get("timeout_ms", 10000)},
        {"action": "screenshot", "label": "after-click"},
    ])
    return steps


def _verify_notification_badge(p: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {"action": "goto", "url": p["url"]},
        {"action": "wait_for", "selector": p["badge_selector"], "timeout_ms": p.get("timeout_ms", 10000)},
        {"action": "assert_visible", "selector": p["badge_selector"]},
    ]
    if p.get("expected_text"):
        steps.append({
            "action": "assert_text",
            "selector": p["badge_selector"],
            "text": p["expected_text"],
        })
    steps.append({"action": "screenshot", "label": "badge"})
    return steps


def _verify_mobile_layout(p: dict[str, Any]) -> list[dict[str, Any]]:
    """Use with task metadata mobile=true."""
    return [
        {"action": "goto", "url": p["url"]},
        {"action": "wait_for", "selector": p.get("main_selector", "main"), "timeout_ms": p.get("timeout_ms", 10000)},
        {"action": "assert_visible", "selector": p.get("nav_selector", "nav")},
        {"action": "screenshot", "label": "mobile-layout"},
    ]
