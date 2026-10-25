"""Generate the AI illustrations shown on restaurant cards in the web frontend.

    python scripts/generate_card_images.py            # only images that are missing
    python scripts/generate_card_images.py --force    # regenerate all of them

Images come from Pollinations (https://pollinations.ai), a free text-to-image service.
The dataset has no photos, so each card shows an illustration for its cuisine or
restaurant type (src/dinewise/web/app.js maps restaurants to these files). They are
generated once and committed with the frontend, so pages never wait on image generation.

The free tier allows one new image every 45 seconds per IP, so a full run takes about
20 minutes. Set POLLINATIONS_TOKEN to use a key without that limit (and without the
watermark).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import httpx

OUT_DIR = Path(__file__).resolve().parent.parent / "src" / "dinewise" / "web" / "images"
ENDPOINT = "https://image.pollinations.ai/prompt/"
WIDTH, HEIGHT = 640, 480
FREE_TIER_GAP_S = 46

# Kept short: heavier "moody, blurred" styling made the model drop the dish itself.
STYLE = "close-up, sharp focus on the food, professional food photography, warm lighting"

# Category -> one prompt per variant. Keep in sync with CARD_IMAGES in app.js.
PROMPTS: dict[str, list[str]] = {
    "brewery": [
        "craft beer flight on a wooden bar counter, copper brewing tanks behind",
        "pints of craft beer and wood-fired pizza at a lively microbrewery taproom",
    ],
    "biryani": [
        "Hyderabadi chicken biryani in a copper handi with raita and boiled egg",
        "mutton dum biryani served on a brass plate with salan",
    ],
    "north-indian": [
        "butter chicken, dal makhani and garlic naan on a dark table",
        "tandoori platter with paneer tikka, kebabs and mint chutney",
    ],
    "south-indian": [
        "crispy masala dosa with coconut chutney and sambar on a banana leaf",
        "South Indian thali with idli, vada, rice and curries on a banana leaf",
    ],
    "chinese": [
        "Indo-Chinese hakka noodles and chilli chicken in bowls with chopsticks",
        "steamed dim sum and momos in bamboo baskets",
    ],
    "italian": [
        "wood-fired margherita pizza on a rustic board, basil and mozzarella",
        "creamy pasta with parmesan and a glass of red wine, candlelight",
    ],
    "burger": [
        "gourmet cheeseburger with fries and a milkshake on a slate board",
    ],
    "cafe": [
        "latte art cappuccino and croissant on a cafe table by the window",
        "cozy specialty coffee cafe interior with plants and pour-over coffee",
    ],
    "desserts": [
        "chocolate sundae with brownie and hot fudge in a tall glass",
        "assorted Indian sweets, gulab jamun and rasmalai, on a brass platter",
    ],
    "bakery": [
        "fresh bakery display with cupcakes, pastries and bread loaves",
    ],
    "seafood": [
        "Kerala fish curry and fried prawns with appam on a banana leaf",
    ],
    "grill": [
        "seekh kebabs and grilled chicken on skewers over glowing charcoal",
    ],
    "asian": [
        "sushi platter and Thai green curry on a dark stone table",
    ],
    "healthy": [
        "colourful healthy salad bowl with avocado, quinoa and fresh vegetables",
    ],
    "beverages": [
        "fresh fruit juices and mocktails in glasses with ice and mint",
    ],
    "dining": [
        "Indian feast spread with paneer butter masala, dal, jeera rice, naan and salad",
        "plated grilled chicken steak with mashed potatoes and vegetables, fine dining",
    ],
}


def image_url(prompt: str, seed: int) -> str:
    params = f"width={WIDTH}&height={HEIGHT}&seed={seed}&nologo=true&model=flux"
    return f"{ENDPOINT}{quote(f'{STYLE}: {prompt}')}?{params}"


def fetch(client: httpx.Client, url: str, token: str | None) -> bytes:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for _attempt in range(15):
        try:
            r = client.get(url, headers=headers)
        except httpx.TransportError as exc:  # timeouts and dropped connections
            print(f"  {type(exc).__name__}; retrying in {FREE_TIER_GAP_S}s", flush=True)
            time.sleep(FREE_TIER_GAP_S)
            continue
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
            return r.content
        if r.status_code in (402, 429, 503):  # free-tier limit, or the service is busy
            match = re.search(r"Retry after (\d+)s", r.text)
            wait = int(match.group(1)) + 2 if match else FREE_TIER_GAP_S
            print(f"  HTTP {r.status_code}; waiting {wait}s", flush=True)
            time.sleep(wait)
            continue
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    raise RuntimeError("gave up after repeated rate limits or busy responses")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true", help="regenerate existing images")
    args = parser.parse_args()

    token = os.environ.get("POLLINATIONS_TOKEN") or None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jobs = [
        (OUT_DIR / f"{category}-{i}.jpg", prompt, 1000 + n * 7 + i)
        for n, (category, prompts) in enumerate(PROMPTS.items())
        for i, prompt in enumerate(prompts, start=1)
    ]
    todo = [job for job in jobs if args.force or not job[0].exists()]
    print(f"{len(todo)} of {len(jobs)} images to generate", flush=True)

    with httpx.Client(timeout=180) as client:
        for k, (path, prompt, seed) in enumerate(todo, start=1):
            started = time.monotonic()
            print(f"[{k}/{len(todo)}] {path.name}", flush=True)
            path.write_bytes(fetch(client, image_url(prompt, seed), token))
            if not token and k < len(todo):
                time.sleep(max(0.0, FREE_TIER_GAP_S - (time.monotonic() - started)))
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
