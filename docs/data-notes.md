# DineWise — Data Notes

Findings from exploring the source dataset (implementation plan §1.1), and the cleaning decisions they led to. Numbers are from revision `5738e9e` of the dataset, explored on 2026-10-21.

## Source

| | |
|---|---|
| Dataset | [ManikaSaini/zomato-restaurant-recommendation](https://huggingface.co/datasets/ManikaSaini/zomato-restaurant-recommendation) |
| Revision | `5738e9eda2fad49ad51c6e0ed26e761d9b947133` (pinned in `config.py`) |
| Files | One CSV, `zomato.csv`, 574 MB |
| Raw rows | 51,717 |
| Columns | 17 (listed below) |
| After cleaning | **12,449 restaurants** |

The pipeline downloads the CSV with `huggingface_hub.hf_hub_download` into `data/raw/`. It does not use `datasets.load_dataset`, which would build a second, Arrow-format copy of a 574 MB file that the pipeline doesn't need.

## Columns

| Raw column | Null % (raw) | Format | Used as |
|---|---|---|---|
| `url` | 0 | `https://www.zomato.com/bangalore/<slug>?context=…` | `city` (from the path) |
| `address` | 0 | Free text, ends in "Bangalore" | `address`, part of the ID |
| `name` | 0 | Free text; 65 names have mojibake | `name`, part of the ID |
| `online_order` | 0 | `Yes` / `No` | `online_order` |
| `book_table` | 0 | `Yes` / `No` | `book_table` |
| `rate` | 15.0 | `4.1/5`, `4.1 /5`, `NEW` (2,208), `-` (69) | `rating`, `is_new` |
| `votes` | 0 | Integer, 0–16,832 | `votes` |
| `phone` | 2.3 | Phone numbers | **Not loaded** (personal data, not needed) |
| `location` | 0.04 | One of **93 neighbourhoods** | `locality` |
| `rest_type` | 0.4 | Comma-separated, 25 distinct values | `rest_types` |
| `dish_liked` | 54.3 | Comma-separated | `dishes_liked` |
| `cuisines` | 0.1 | Comma-separated, 107 distinct values (106 after aliasing) | `cuisines` |
| `approx_cost(for two people)` | 0.7 | `300`, `1,200` (all numeric after removing commas) | `cost_for_two` |
| `reviews_list` | 0 | Python-literal list of `('Rated 4.0', 'RATED\n  text')`; up to 1.3 MB per row; 14.7% are `[]` | `review_snippets` |
| `menu_item` | 0 | Almost always `[]` | **Not loaded** |
| `listed_in(type)` | 0 | 7 values: Delivery, Dine-out, Desserts, Cafes, Drinks & nightlife, Buffet, Pubs and bars | `listing_types`, tags |
| `listed_in(city)` | 0 | **30 Zomato zones** (for example "Koramangala 5th Block", "BTM") | `zones` |

## Decision: "Location" means a Bangalore neighbourhood

- Every URL is under `zomato.com/bangalore/`, and 96% of addresses contain "Bangalore". **The dataset is Bangalore-only.**
- So "Location" in the UI and API means a **neighbourhood**: one of the 93 `location` values (Indiranagar, BTM, Whitefield, …). The `city` field is still stored ("Bangalore"), so other cities can be added later.
- A request for "Delhi" or "Mumbai" has to be answered with a clear "Bangalore only" message (edge case INP-06).
- Neighbourhood sizes vary a lot: from 885 restaurants (BTM) down to 1 (Kengeri, Peenya, Jakkur). Small neighbourhoods will often need relaxation in Phase 2.
- Some neighbourhoods overlap: there's a generic "Koramangala" plus "Koramangala 1st–8th Block". Phase 2's location matching should treat a query for "Koramangala" as covering all of its blocks.

## Zones (`listed_in(city)`) are listing areas, not neighbourhoods

- `listed_in(city)` disagrees with `location` in 69% of raw rows. It is the Zomato area a listing appeared under, not where the restaurant is.
- One restaurant can be listed in many zones (median 2, max 15). Only 76% of restaurants have their own neighbourhood among their zones.
- **For Phase 2 (edge cases D-01, D-04):** don't treat zones as a searchable location. They are a good signal for the "widen location" relaxation step, though. "Restaurants listed in the same zones as the requested neighbourhood" is a reasonable, data-driven meaning of "nearby" that needs no coordinates.
- When `location` is blank (10 restaurants), the first zone is used as the locality (DATA-31).

## Duplicates

- 51,717 rows describe **12,450 distinct (name, address) pairs** once case, punctuation, and whitespace are normalized. That's 44 more merges than exact matching finds.
- Each restaurant appears about 4 times on average (up to 34): once per `listed_in(type)` × zone combination.
- **The copies were scraped at different times.** Within a group, the rating differs in 5,256 groups, votes in 3,936, cost in 345, and location in 28. The pipeline keeps the row with the **most votes** (the freshest snapshot), and merges listing types and zones across the group.
- Chains are kept as separate branches, because they have different addresses: Cafe Coffee Day has 54, Domino's 38, Pizza Hut 37. The UI needs to show the locality to tell them apart (UI-13). The shortlist may need a branch cap (D-07).

## Ratings

- After dedup: 9,436 rated (mean 3.63, median 3.7, range 1.8–4.9), **657 `NEW`**, and 2,356 with no rating at all. That's 24% unrated in total.
- No out-of-range or malformed ratings were found; the parser still guards against them.
- Since a quarter of restaurants are unrated, `min_rating > 0` excludes a lot. How unrated restaurants are scored (D-06) matters.

## Cost and budget bands

Cost for two after dedup: median ₹400; 25th percentile ₹250, 75th ₹600, 90th ₹800, 99th ₹2,000; max ₹6,000. 66 restaurants have no cost.

| Thresholds (low ≤ a < medium ≤ b < high) | Low | Medium | High |
|---|---|---|---|
| 500 / 1,500 (original architecture) | 72.4% | 25.3% | **2.3%** (289) |
| **400 / 1,000 (adopted)** | 59.2% | 34.4% | 6.4% (797) |
| 300 / 800 | 41.3% | 48.7% | 10.0% |

**Decision:** the defaults changed to **400 / 1,000**. With 500 / 1,500, "high" had only 289 restaurants across 93 neighbourhoods, so almost every "high" query would have relied on relaxation. 400 / 1,000 still matches what the words mean for Bangalore prices (under ₹400 for two is cheap; over ₹1,000 is upmarket). Override them with `DINEWISE_BUDGET_LOW_MAX` / `_MEDIUM_MAX`. `budget_band` is stored in the Parquet file and the thresholds are written to `meta.json`, so a change in thresholds means re-running ingestion (D-10).

## Text quality

- **Mojibake:** 65 names (and many reviews) are UTF-8 that was mis-decoded as Latin-1 **up to four times**, so "Café" became "CafÃƒÂƒÃ‚Â©…". `fix_mojibake` reverses the encoding repeatedly until the text is valid, then maps stray Windows-1252 bytes ("Brother\x92s" → "Brother's"). All 65 names are repaired.
- No HTML entities were found in names; `html.unescape` is applied anyway.
- Some reviews contain `??` where emoji used to be. They were lost upstream and can't be recovered.
- 0.6% of reviews contain something that looks like a phone number or email. Snippets are scrubbed (SEC-07).

## Reviews

- Parsed with `ast.literal_eval`, which evaluates literals only and never runs code. There were no parse failures in a 3,000-row sample.
- Up to 3 snippets per restaurant, each up to 200 characters, cut at a word boundary. Reviews shorter than 20 characters are skipped.
- 20% of restaurants end up with no snippets.

## Tags

Derived from `rest_types`, `listing_types`, `book_table`, and `online_order`. Counts over 12,449 restaurants:

| Tag | Rule | Count |
|---|---|---|
| `delivery` | `online_order`, or listed under Delivery | 9,716 |
| `quick_service` | Quick Bites, Food Court, Kiosk, Takeaway, Food Truck, Delivery | 7,394 |
| `family_friendly` | Casual or Fine Dining, and not a Bar / Pub / Microbrewery / Lounge / Club (heuristic) | 2,359 |
| `desserts` | Dessert Parlor, Bakery, Sweet Shop, Confectionery, or listed under Desserts | 1,813 |
| `table_booking` | `book_table` | 949 |
| `cafe` | Cafe, Irani Cafe, or listed under Cafes | 760 |
| `bar` | Bar, Pub, Microbrewery, Lounge, Club (rest type only) | 559 |
| `buffet` | Listed under Buffet | 340 |
| `fine_dining` | Fine Dining | 91 |

`bar` uses the restaurant type only. The "Pubs and bars" and "Drinks & nightlife" listings include juice and ice-cream shops, which would otherwise be tagged as bars.

## Pipeline summary (current build)

```
rows in:                51,717
dropped (no name/addr):      0
dropped (no locality):       0
duplicates removed:     39,268
rows out:               12,449
  no rating:             3,013  (new: 657, invalid: 0)
  no cost:                  66
  budget bands:       low 7,353, medium 4,234, high 796
localities: 93, zones: 30, cuisines: 106
```

It runs in about 15 seconds on a laptop. Rebuilding from the same input and settings produces the same build ID.
