"""
Q-MisinfoGuard: PHEME Dataset -> SQLite Loader

Migrates all PHEME threads, tweets, interaction edges, and fact-checking links
into a high-performance, normalized SQLite database.

Schema highlights:
  - PRAGMA foreign_keys = ON enforced on all connections
  - threads: thread_id (folder name), source_tweet_id (structure.json root),
             raw true_flag and misinformation, nullable veracity
  - tweets: composite primary key (thread_id, tweet_id), INSERT OR IGNORE,
            created_ms with Twitter Snowflake fallback, optional raw_json
  - interactions: edges derived strictly from structure.json, depth column,
                  interaction_type ('reply', 'retweet', 'quote'), UNIQUE(thread_id, child_id)
  - evidence_links: fact-checking URLs, mediatypes, and positions
  - Indexes on threads(event, veracity) and tweets(user_id)

Note on features:
  Followers, retweet, and favorite counts are collection-time snapshots,
  so they should NOT be treated as dynamic features reflecting cascade state over time.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

TWITTER_TIME_FORMAT = "%a %b %d %H:%M:%S %z %Y"
TWITTER_SNOWFLAKE_EPOCH = 1288834974657  # Twitter custom epoch (ms): Nov 04 2010 01:42:54 UTC


def get_db_connection(db_path: Path) -> sqlite3.Connection:
    """Create a database connection with foreign key enforcement enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection):
    """Initialize the revised SQLite schema and indexes."""
    with conn:
        conn.executescript("""
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS threads (
            thread_id TEXT PRIMARY KEY,
            source_tweet_id TEXT,
            event TEXT NOT NULL,
            is_rumour TEXT NOT NULL,
            category TEXT,
            veracity TEXT,
            misinformation INTEGER,
            true_flag TEXT,
            is_turnaround INTEGER
        );

        CREATE TABLE IF NOT EXISTS tweets (
            thread_id TEXT NOT NULL,
            tweet_id TEXT NOT NULL,
            user_id TEXT,
            screen_name TEXT,
            followers_count INTEGER,
            verified INTEGER,
            text TEXT,
            created_at TEXT,
            created_ms INTEGER NOT NULL,
            retweet_count INTEGER,
            favorite_count INTEGER,
            is_source INTEGER NOT NULL,
            in_reply_to_tweet_id TEXT,
            in_reply_to_user_id TEXT,
            raw_json TEXT,
            PRIMARY KEY (thread_id, tweet_id),
            FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            parent_id TEXT NOT NULL,
            child_id TEXT NOT NULL,
            parent_type TEXT,
            interaction_type TEXT NOT NULL,
            depth INTEGER NOT NULL,
            created_ms INTEGER,
            timestamp TEXT,
            UNIQUE(thread_id, child_id),
            FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS evidence_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id TEXT NOT NULL,
            url TEXT,
            mediatype TEXT,
            position TEXT,
            FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
        );

        -- Performance Indexes
        CREATE INDEX IF NOT EXISTS idx_threads_event_veracity ON threads(event, veracity);
        CREATE INDEX IF NOT EXISTS idx_tweets_user_id ON tweets(user_id);
        CREATE INDEX IF NOT EXISTS idx_tweets_thread ON tweets(thread_id);
        CREATE INDEX IF NOT EXISTS idx_interactions_thread ON interactions(thread_id);
        CREATE INDEX IF NOT EXISTS idx_interactions_parent ON interactions(parent_id);
        CREATE INDEX IF NOT EXISTS idx_interactions_child ON interactions(child_id);
        CREATE INDEX IF NOT EXISTS idx_evidence_links_thread ON evidence_links(thread_id);
        """)


def decode_snowflake_ms(tweet_id_str: str) -> Optional[int]:
    """Extract creation timestamp in milliseconds from a 64-bit Twitter Snowflake ID."""
    try:
        tid = int(tweet_id_str)
        return (tid >> 22) + TWITTER_SNOWFLAKE_EPOCH
    except (ValueError, TypeError):
        return None


def parse_timestamp_and_ms(raw_created_at: Optional[str], tweet_id_str: str) -> Tuple[str, int]:
    """
    Convert raw Twitter created_at to (ISO 8601 string, timestamp_ms).
    Falls back to Twitter Snowflake decode if created_at is missing, empty, or unparseable.
    """
    if raw_created_at:
        try:
            dt = datetime.strptime(raw_created_at, TWITTER_TIME_FORMAT)
            ms = int(dt.timestamp() * 1000)
            return dt.isoformat(), ms
        except (ValueError, TypeError):
            pass

    # Fallback to Snowflake ID calculation
    ms = decode_snowflake_ms(tweet_id_str)
    if ms is not None:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        return dt.isoformat(), ms

    return "", 0


def load_json_file(path: Path) -> dict:
    """Safely load JSON, returning empty dict if missing, empty, or corrupt."""
    if not path.exists() or path.name.startswith("._") or path.stat().st_size == 0:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def derive_veracity_label(is_rumour: str, annotation: dict) -> Optional[str]:
    """
    Derives veracity label ('true', 'false', 'unverified', or None).
    Per specification:
      - Non-rumours -> None (NULL)
      - Contradictory labels (misinformation=1, true=1) -> None (NULL)
    """
    if is_rumour != "rumour" or not annotation:
        return None

    true_raw = annotation.get("true")
    misinfo_raw = annotation.get("misinformation")

    # Direct string label format
    if isinstance(true_raw, str) and true_raw.lower() in ("true", "false", "unverified"):
        return true_raw.lower()

    # Numeric flag logic per PHEME decision matrix
    if misinfo_raw is not None and true_raw is not None:
        try:
            misinfo = int(misinfo_raw)
            true_flag = int(true_raw)
            if misinfo == 0 and true_flag == 0:
                return "unverified"
            if misinfo == 0 and true_flag == 1:
                return "true"
            if misinfo == 1 and true_flag == 0:
                return "false"
            if misinfo == 1 and true_flag == 1:
                return None  # Contradictory -> NULL
        except (ValueError, TypeError):
            return None

    if misinfo_raw is not None and true_raw is None:
        try:
            misinfo = int(misinfo_raw)
            return "unverified" if misinfo == 0 else "false"
        except (ValueError, TypeError):
            return None

    return None


def infer_interaction_type(tweet_data: dict) -> str:
    """Infer interaction type ('retweet', 'quote', or 'reply')."""
    if not tweet_data:
        return "reply"
    if tweet_data.get("retweeted_status") is not None:
        return "retweet"
    if tweet_data.get("quoted_status") is not None or tweet_data.get("is_quote_status") is True:
        return "quote"
    return "reply"


def flatten_structure(
    tree: Any,
    parent_id: str,
    depth: int,
    edges: List[Tuple[str, str, int]],
    seen_children: Set[str]
):
    """
    Recursively walks structure.json's reply tree, flattening to (child_id, parent_id, depth).
    structure.json is the single source of truth for parent/child hierarchy.
    """
    if not isinstance(tree, dict):
        return
    for child_id, children in tree.items():
        if child_id not in seen_children:
            seen_children.add(child_id)
            edges.append((child_id, parent_id, depth))
        if isinstance(children, dict) and children:
            flatten_structure(children, child_id, depth + 1, edges, seen_children)


def process_thread(
    thread_dir: Path,
    label: str,
    event_name: str,
    include_raw_json: bool
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Process a single thread folder and extract threads, tweets, interactions, and links."""
    thread_id = thread_dir.name
    annotation_path = thread_dir / "annotation.json"
    structure_path = thread_dir / "structure.json"

    source_dir = thread_dir / "source-tweets"
    if not source_dir.exists():
        source_dir = thread_dir / "source-tweet"

    reactions_dir = thread_dir / "reactions"

    # 1. Annotation
    annotation = load_json_file(annotation_path)
    is_rumour_clean = "rumour" if label == "rumours" else "non-rumour"
    veracity = derive_veracity_label(is_rumour_clean, annotation)
    misinfo_raw = annotation.get("misinformation")
    true_raw = annotation.get("true")
    is_turnaround = annotation.get("is_turnaround")

    try:
        misinformation = int(misinfo_raw) if misinfo_raw is not None else None
    except (ValueError, TypeError):
        misinformation = None

    true_flag = str(true_raw) if true_raw is not None else None

    # 2. Structure
    structure = load_json_file(structure_path)
    source_tweet_id = None
    direct_replies = {}

    if structure:
        if thread_id in structure:
            source_tweet_id = thread_id
            direct_replies = structure[thread_id]
        elif len(structure) == 1:
            source_tweet_id = next(iter(structure.keys()))
            direct_replies = structure[source_tweet_id]
        else:
            # Multi-key structure: pick key with non-empty dict or first key
            keys_with_children = [k for k, v in structure.items() if isinstance(v, dict) and len(v) > 0]
            source_tweet_id = keys_with_children[0] if keys_with_children else next(iter(structure.keys()))
            direct_replies = structure[source_tweet_id]
    else:
        source_tweet_id = thread_id

    # Fallback to source tweet file if structure had no keys
    source_files = [f for f in source_dir.glob("*.json") if not f.name.startswith("._")] if source_dir.exists() else []
    if not source_tweet_id and source_files:
        source_tweet_id = source_files[0].stem

    thread_row = {
        "thread_id": thread_id,
        "source_tweet_id": source_tweet_id,
        "event": event_name,
        "is_rumour": is_rumour_clean,
        "category": annotation.get("category"),
        "veracity": veracity,
        "misinformation": misinformation,
        "true_flag": true_flag,
        "is_turnaround": int(is_turnaround) if is_turnaround is not None else None
    }

    # 3. Evidence links
    link_rows = []
    if annotation and "links" in annotation and isinstance(annotation["links"], list):
        for link_obj in annotation["links"]:
            if isinstance(link_obj, dict):
                link_rows.append({
                    "thread_id": thread_id,
                    "url": link_obj.get("link"),
                    "mediatype": link_obj.get("mediatype"),
                    "position": link_obj.get("position")
                })

    # 4. Tweets (source + reactions)
    tweet_rows = []
    reaction_data_map: Dict[str, dict] = {}

    # Source tweet
    if source_files:
        source_file = source_files[0]
        s_data = load_json_file(source_file)
        if s_data:
            s_user = s_data.get("user", {})
            iso_time, ms_time = parse_timestamp_and_ms(s_data.get("created_at"), source_file.stem)
            tweet_rows.append({
                "thread_id": thread_id,
                "tweet_id": source_file.stem,
                "user_id": str(s_user.get("id_str", s_user.get("id", ""))),
                "screen_name": s_user.get("screen_name"),
                "followers_count": s_user.get("followers_count"),
                "verified": 1 if s_user.get("verified") else 0,
                "text": s_data.get("text"),
                "created_at": iso_time,
                "created_ms": ms_time,
                "retweet_count": s_data.get("retweet_count"),
                "favorite_count": s_data.get("favorite_count"),
                "is_source": 1,
                "in_reply_to_tweet_id": str(s_data.get("in_reply_to_status_id_str", s_data.get("in_reply_to_status_id") or "")),
                "in_reply_to_user_id": str(s_data.get("in_reply_to_user_id_str", s_data.get("in_reply_to_user_id") or "")),
                "raw_json": json.dumps(s_data, ensure_ascii=False) if include_raw_json else None
            })

    # Reaction tweets
    if reactions_dir.exists():
        for r_file in reactions_dir.glob("*.json"):
            if r_file.name.startswith("._"):
                continue
            r_data = load_json_file(r_file)
            if not r_data:
                continue
            reaction_data_map[r_file.stem] = r_data
            r_user = r_data.get("user", {})
            iso_time, ms_time = parse_timestamp_and_ms(r_data.get("created_at"), r_file.stem)
            tweet_rows.append({
                "thread_id": thread_id,
                "tweet_id": r_file.stem,
                "user_id": str(r_user.get("id_str", r_user.get("id", ""))),
                "screen_name": r_user.get("screen_name"),
                "followers_count": r_user.get("followers_count"),
                "verified": 1 if r_user.get("verified") else 0,
                "text": r_data.get("text"),
                "created_at": iso_time,
                "created_ms": ms_time,
                "retweet_count": r_data.get("retweet_count"),
                "favorite_count": r_data.get("favorite_count"),
                "is_source": 0,
                "in_reply_to_tweet_id": str(r_data.get("in_reply_to_status_id_str", r_data.get("in_reply_to_status_id") or "")),
                "in_reply_to_user_id": str(r_data.get("in_reply_to_user_id_str", r_data.get("in_reply_to_user_id") or "")),
                "raw_json": json.dumps(r_data, ensure_ascii=False) if include_raw_json else None
            })

    # 5. Interactions (edges strictly from structure.json)
    interaction_rows = []
    edges: List[Tuple[str, str, int]] = []
    seen_children: Set[str] = set()

    if isinstance(direct_replies, dict):
        flatten_structure(direct_replies, source_tweet_id, 1, edges, seen_children)

    for child_id, parent_id, depth in edges:
        child_data = reaction_data_map.get(child_id)
        itype = infer_interaction_type(child_data)
        parent_type = "post" if parent_id == source_tweet_id else "reply"

        if child_data:
            iso_time, ms_time = parse_timestamp_and_ms(child_data.get("created_at"), child_id)
        else:
            # Tweet JSON missing on disk (deleted/protected); derive timestamp from Snowflake ID
            iso_time, ms_time = parse_timestamp_and_ms(None, child_id)

        interaction_rows.append({
            "thread_id": thread_id,
            "parent_id": parent_id,
            "child_id": child_id,
            "parent_type": parent_type,
            "interaction_type": itype,
            "depth": depth,
            "created_ms": ms_time,
            "timestamp": iso_time
        })

    return thread_row, tweet_rows, interaction_rows, link_rows


def migrate(pheme_root: Path, db_path: Path, events: Optional[List[str]] = None, include_raw_json: bool = False):
    """Walks the PHEME dataset, loads all data into SQLite, and outputs diagnostics."""
    print(f"Connecting to SQLite database: {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db_connection(db_path)
    init_schema(conn)

    event_dirs = [
        d for d in sorted(pheme_root.iterdir())
        if d.is_dir() and not d.name.startswith(".")
    ]
    if events:
        event_dirs = [d for d in event_dirs if any(e in d.name for e in events)]

    total_attempted_tweets = 0
    total_attempted_edges = 0

    thread_batch = []
    tweet_batch = []
    interaction_batch = []
    link_batch = []

    def flush_batches():
        nonlocal thread_batch, tweet_batch, interaction_batch, link_batch
        with conn:
            if thread_batch:
                conn.executemany("""
                INSERT OR REPLACE INTO threads (
                    thread_id, source_tweet_id, event, is_rumour, category,
                    veracity, misinformation, true_flag, is_turnaround
                ) VALUES (
                    :thread_id, :source_tweet_id, :event, :is_rumour, :category,
                    :veracity, :misinformation, :true_flag, :is_turnaround
                )""", thread_batch)
                thread_batch = []

            if tweet_batch:
                conn.executemany("""
                INSERT OR IGNORE INTO tweets (
                    thread_id, tweet_id, user_id, screen_name, followers_count,
                    verified, text, created_at, created_ms, retweet_count,
                    favorite_count, is_source, in_reply_to_tweet_id, in_reply_to_user_id, raw_json
                ) VALUES (
                    :thread_id, :tweet_id, :user_id, :screen_name, :followers_count,
                    :verified, :text, :created_at, :created_ms, :retweet_count,
                    :favorite_count, :is_source, :in_reply_to_tweet_id, :in_reply_to_user_id, :raw_json
                )""", tweet_batch)
                tweet_batch = []

            if interaction_batch:
                conn.executemany("""
                INSERT OR IGNORE INTO interactions (
                    thread_id, parent_id, child_id, parent_type,
                    interaction_type, depth, created_ms, timestamp
                ) VALUES (
                    :thread_id, :parent_id, :child_id, :parent_type,
                    :interaction_type, :depth, :created_ms, :timestamp
                )""", interaction_batch)
                interaction_batch = []

            if link_batch:
                conn.executemany("""
                INSERT INTO evidence_links (
                    thread_id, url, mediatype, position
                ) VALUES (
                    :thread_id, :url, :mediatype, :position
                )""", link_batch)
                link_batch = []

    print(f"Beginning ingestion across {len(event_dirs)} event directories...")
    for ev_dir in event_dirs:
        clean_event = ev_dir.name.replace("-all-rnr-threads", "")
        print(f"  -> Processing event: {clean_event} ({ev_dir.name})")

        for label in ("rumours", "non-rumours"):
            cat_dir = ev_dir / label
            if not cat_dir.exists():
                continue

            threads = [t for t in cat_dir.iterdir() if t.is_dir() and not t.name.startswith(".")]
            for t_dir in threads:
                t_row, tw_rows, i_rows, l_rows = process_thread(t_dir, label, clean_event, include_raw_json)

                thread_batch.append(t_row)
                tweet_batch.extend(tw_rows)
                interaction_batch.extend(i_rows)
                link_batch.extend(l_rows)

                total_attempted_tweets += len(tw_rows)
                total_attempted_edges += len(i_rows)

                if len(tweet_batch) >= 5000:
                    flush_batches()

    flush_batches()
    print("\nIngestion complete. Running post-load audit queries...\n")

    # Audit 1: Ignored duplicate tweets
    actual_tweets_count = conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
    ignored_duplicate_tweets = total_attempted_tweets - actual_tweets_count

    # Audit 2: Edges with no tweet row (LEFT JOIN)
    edges_no_tweet_count = conn.execute("""
        SELECT COUNT(*)
        FROM interactions i
        LEFT JOIN tweets t ON i.thread_id = t.thread_id AND i.child_id = t.tweet_id
        WHERE t.tweet_id IS NULL
    """).fetchone()[0]

    # Audit 3: SELECT DISTINCT position FROM evidence_links
    distinct_positions = [
        row[0] for row in conn.execute("SELECT DISTINCT position FROM evidence_links").fetchall()
    ]

    total_threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    total_interactions = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]
    total_links = conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0]

    print(f"Total threads loaded       : {total_threads}")
    print(f"Total tweets in database   : {actual_tweets_count}")
    print(f"Attempted tweet inserts    : {total_attempted_tweets}")
    print(f"Ignored duplicate tweets   : {ignored_duplicate_tweets}")
    print(f"Total interaction edges    : {total_interactions}")
    print(f"Edges with no tweet row    : {edges_no_tweet_count}")
    print(f"Total evidence links       : {total_links}")
    print(f"Distinct link positions    : {distinct_positions}")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate PHEME dataset to SQLite")
    parser.add_argument(
        "--pheme-root",
        default="/home/guru-saran/Documents/idp2026/all-rnr-annotated-threads",
        help="Path to PHEME all-rnr-annotated-threads directory"
    )
    parser.add_argument(
        "--db-path",
        default="/home/guru-saran/Documents/idp2026/data/processed/pheme.db",
        help="Target SQLite database file path"
    )
    parser.add_argument(
        "--events",
        nargs="*",
        default=None,
        help="Specific events to process (default: all events)"
    )
    parser.add_argument(
        "--include-raw-json",
        action="store_true",
        default=False,
        help="Store complete tweet raw JSON in the tweets table"
    )

    args = parser.parse_args()
    migrate(Path(args.pheme_root), Path(args.db_path), args.events, args.include_raw_json)
