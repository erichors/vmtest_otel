#!/usr/bin/env python3
"""
browser_load.py - browser-driven load for the dt-demo checkout page.

Unlike loadgen.py (plain HTTP, runs no JavaScript), this drives real headless
Chromium sessions through the page served by checkout-java, so the Dynatrace
RUM JavaScript executes and the page's fetch() calls carry RUM trace context
into the OTel-instrumented tiers.

Setup:
  pip install playwright && playwright install chromium

Usage:
  browser_load.py [--url http://localhost:8080/] [--users 5] [--duration 300]
                  [--think 1.0]
Each simulated user loops: load page -> place order -> view recent orders
(sometimes the revenue report), pausing --think seconds (jittered) between
actions, with a fresh browser context (new RUM session) per loop.
"""

import argparse
import asyncio
import random
import time
from collections import Counter

from playwright.async_api import async_playwright

SKU_PREFIXES = ["ELEC", "APP", "HOME", "GRO", "SPT"]
SKUS = [f"{p}-{i:03d}" for p in SKU_PREFIXES for i in range(1, 9)]
CUSTOMER_IDS = list(range(1, 26))


async def think(base):
    await asyncio.sleep(base * random.uniform(0.5, 1.5))


async def user_loop(browser, args, deadline, stats):
    while time.time() < deadline:
        context = await browser.new_context()
        page = await context.new_page()
        try:
            await page.goto(args.url, wait_until="load")
            await think(args.think)

            await page.fill("#customerId", str(random.choice(CUSTOMER_IDS)))
            await page.fill("#sku", random.choice(SKUS))
            await page.fill("#qty", str(random.randint(1, 3)))
            async with page.expect_response("**/api/orders") as resp:
                await page.click("#place-order")
            stats[(await resp.value).status] += 1
            await think(args.think)

            async with page.expect_response("**/api/orders/recent*"):
                await page.click("#view-recent")
            await think(args.think)

            if random.random() < 0.2:
                async with page.expect_response("**/api/reports/revenue"):
                    await page.click("#view-report")
                await think(args.think)
        except Exception as e:
            stats["error"] += 1
            print(f"user error: {e}")
        finally:
            await context.close()  # closing the context ends the RUM session


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8080/")
    ap.add_argument("--users", type=int, default=5)
    ap.add_argument("--duration", type=int, default=300, help="seconds")
    ap.add_argument("--think", type=float, default=1.0, help="mean think time, seconds")
    args = ap.parse_args()

    stats = Counter()
    deadline = time.time() + args.duration
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        await asyncio.gather(*(user_loop(browser, args, deadline, stats) for _ in range(args.users)))
        await browser.close()
    print("order responses / errors:", dict(stats))


if __name__ == "__main__":
    asyncio.run(main())
