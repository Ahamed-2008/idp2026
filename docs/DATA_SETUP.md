# PHEME Dataset Setup & Database Rebuild Guide

This guide details how to download the raw PHEME dataset and rebuild the SQLite database (`data/processed/pheme.db`) locally from scratch.

---

## 1. Dataset Origin & Download Links

The raw conversational thread data is sourced from the expanded **9-Event PHEME Dataset for Rumour Detection and Veracity Classification**.

* **Figshare DOI**: [10.6084/m9.figshare.6392078](https://doi.org/10.6084/m9.figshare.6392078)
* **Figshare Page**: [PHEME dataset for Rumour Detection and Veracity Classification](https://figshare.com/articles/dataset/PHEME_dataset_for_Rumour_Detection_and_Veracity_Classification/6392078)
* **Archive File**: `all-rnr-annotated-threads.tar.bz2` 
* **Authors**: Elena Kochkina, Maria Liakata, Arkaitz Zubiaga (2018)

### The 9 Breaking News Events Included:
1. `charliehebdo` (Charlie Hebdo Paris attack)
2. `ebola-essien` (Michael Essien Ebola rumor)
3. `ferguson` (Michael Brown shooting & Ferguson unrest)
4. `germanwings-crash` (Germanwings Flight 9525 crash)
5. `gurlitt` (Cornelius Gurlitt Nazi art discovery)
6. `ottawashooting` (Ottawa Parliament Hill shootings)
7. `prince-toronto` (Prince Toronto concert rumor)
8. `putinmissing` (Vladimir Putin disappearance rumor)
9. `sydneysiege` (Sydney Lindt Cafe hostage crisis)

---

## 2. Prerequisites

* **Python 3.9+**
* **Zero external dependencies required**: The database loader uses Python's standard library (`sqlite3`, `pathlib`, `json`, `datetime`, `argparse`).

---

## 3. Step-by-Step Rebuild Instructions

### Step 1: Download the Archive
Download `all-rnr-annotated-threads.tar.bz2` from Figshare into `data/raw/`:

### Step 2: Extract the Data
Extract the archive directly into `data/raw/`:

```bash
tar -xjf all-rnr-annotated-threads.tar.bz2
```

This will produce the folder `data/raw/all-rnr-annotated-threads/` (or in the root folder, depending on extraction).

### Step 3: Run the Ingestion Script
From the project root, run the loader:

```bash
python src/data/load_pheme_db.py \
  --pheme-root data/raw/all-rnr-annotated-threads \
  --db-path data/processed/pheme.db
```

#### Available Command-Line Options:
* `--pheme-root PATH`: Path to the raw `all-rnr-annotated-threads` folder (default: `all-rnr-annotated-threads` or `data/raw/all-rnr-annotated-threads`).
* `--db-path PATH`: Destination path for the `.db` file (default: `data/processed/pheme.db`).
* `--events [EVENT ...]`: Process only specific events (e.g., `--events charliehebdo ferguson`).
* `--include-raw-json`: Optional flag to store raw JSON string payloads inside `tweets.raw_json` (increases DB size from ~25 MB to ~250 MB).

---

## 4. Expected Output & Sanity Checks

Upon completion, the loader executes an automated verification audit. Verify your output matches the expected figures below:

```text
Total threads loaded       : 6425
Total tweets in database   : 104582
Attempted tweet inserts    : 105354
Ignored duplicate tweets   : 772
Total interaction edges    : 94915
Edges with no tweet row    : 1
Total evidence links       : 5083
Distinct link positions    : ['for', 'observing', 'against', None]
```

### Veracity Breakdown in `threads` Table:
* **Non-rumours**: `4,023` (veracity is `NULL`)
* **Rumours (True)**: `1,067`
* **Rumours (False)**: `638`
* **Rumours (Unverified)**: `697`

---

## 5. Schema & Analytical Design Notes

1. **Foreign Key Integrity**:
   Every connection executes `PRAGMA foreign_keys = ON;`. Deleting a thread will cascade-delete its tweets, edges, and evidence links.
2. **Parent/Child Ground Truth**:
   The `interactions` table is populated strictly from `structure.json`. The Twitter API field `in_reply_to_tweet_id` is **not** used to determine conversational hierarchy.
3. **Snowflake Timestamp Recovery**:
   When tweet timestamps are unparseable or deleted from Twitter, creation times (`created_ms`) are recovered directly from the 64-bit Twitter Snowflake ID:
   $$\text{created\_ms} = (\text{tweet\_id} \gg 22) + 1288834974657$$
4. **Snapshot vs Dynamic Features**:
   `followers_count`, `retweet_count`, and `favorite_count` in the `tweets` table represent **collection-time snapshots** when researchers crawled the data, not real-time states as the cascade evolved. To study propagation dynamics over time, rely on `depth` and `created_ms`.
5. **Left Joins for Deleted Tweets**:
   Always use `LEFT JOIN` when joining `interactions` to `tweets`:
   ```sql
   SELECT i.thread_id, i.parent_id, i.child_id, i.depth, i.interaction_type, t.text
   FROM interactions i
   LEFT JOIN tweets t ON i.thread_id = t.thread_id AND i.child_id = t.tweet_id;
   ```
   This ensures deleted or suspended tweets that exist as structural reply nodes in `structure.json` are not dropped from propagation models.

---

## 6. Quick Verification SQL

You can test the database from Python:

```python
import sqlite3

conn = sqlite3.connect("data/processed/pheme.db")
conn.execute("PRAGMA foreign_keys = ON;")

# Query deep cascades of false rumors
query = """
SELECT t.thread_id, t.event, t.category, MAX(i.depth) AS max_depth, COUNT(i.child_id) AS total_replies
FROM threads t
JOIN interactions i ON t.thread_id = i.thread_id
WHERE t.veracity = 'false'
GROUP BY t.thread_id
ORDER BY max_depth DESC
LIMIT 5;
"""

for row in conn.execute(query):
    print(row)
```
