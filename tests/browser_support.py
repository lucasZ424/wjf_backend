"""Helpers for tests that drive a real browser; the fixtures (`browser`, `rig`, `page`) are in conftest.py."""


def sign_in(page, rig, who="viewer"):
    page.goto(rig.url)
    page.locator("#credential").fill(rig.credentials[who])
    page.locator("#login-form button").click()
    page.locator("#workspace").wait_for(state="visible")
