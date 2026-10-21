"""Offline browser benchmark: no accounts, tokens or real ChatGPT traffic."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chat_backup.automation import create_share_link, copy_shared_conversation, delete_new_share_link


SOURCE = """<button data-testid="share-chat-button" onclick="dialog.hidden=false">Share</button>
<div role="dialog" id="dialog" hidden>
  <input id="link" value="">
  <button onclick="link.value='https://chatgpt.com/share/synthetic'">Create link</button>
  <button data-testid="delete-shared-link" onclick="confirmButton.hidden=false">Delete link</button>
  <button id="confirmButton" hidden onclick="dialog.hidden=true">Confirm</button>
</div>"""
TARGET = """<button id="continueButton" onclick="this.remove(); composer.hidden=false">Continue this conversation</button>
<div id="composer" hidden><textarea id="prompt-textarea"></textarea>
<button data-testid="send-button" onclick="history.pushState({}, '', '/c/synthetic-target')">Send</button></div>"""


def benchmark(repeats: int) -> dict:
    samples = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(service_workers="block")
        navigations = []

        def fixture(route):
            if not route.request.is_navigation_request():
                route.abort()
                return
            navigations.append(route.request.url)
            # Fixed synthetic document latency, identical for baseline and winner.
            time.sleep(0.15)
            route.fulfill(status=200, content_type="text/html", body=(
                TARGET if "/share/" in route.request.url else SOURCE
            ))

        context.route("**/*", fixture)
        source, target = context.new_page(), context.new_page()
        for _ in range(repeats):
            navigations.clear()
            started = time.perf_counter()
            share_url, created = create_share_link(source, "synthetic-source", threading.Event())
            shared = time.perf_counter()
            target_url = copy_shared_conversation(target, share_url, "Synthetic copy", threading.Event())
            copied = time.perf_counter()
            cleanup_ok = delete_new_share_link(source, "synthetic-source", threading.Event())
            finished = time.perf_counter()
            assert created and cleanup_ok and target_url.endswith("/c/synthetic-target")
            samples.append({
                "share_ms": round((shared - started) * 1000, 2),
                "copy_ms": round((copied - shared) * 1000, 2),
                "cleanup_ms": round((finished - copied) * 1000, 2),
                "total_ms": round((finished - started) * 1000, 2),
                "document_navigations": len(navigations),
            })
        browser.close()
    return {
        "environment": "offline Playwright Chromium; 150ms/document; Continue-only shared page",
        "scope": "UI flow only; excludes login, real network and unchanged 20-35s success cooldown",
        "repeats": repeats,
        "median": {key: statistics.median(sample[key] for sample in samples) for key in samples[0]},
        "samples": samples,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10:
        parser.error("repeats must be between 1 and 10")
    print(json.dumps(benchmark(args.repeats), indent=2))
