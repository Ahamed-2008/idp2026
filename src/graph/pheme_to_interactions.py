"""
Q-MisinfoGuard: PHEME -> interactions.csv converter

Walks a PHEME dataset directory and produces:
  1. interactions.csv   - reply/retweet edges, matching the schema expected
                           by build_propagation_graph.py
  2. veracity_labels.json - ground-truth veracity per source tweet
                             (true / false / unverified), useful later for
                             evaluating your teammate's detection model,
                             NOT the same thing as the model's own JSON output

Expected input layout (after unzipping the PHEME figshare download):

  <pheme_root>/
    charliehebdo/
      rumours/
        <source_tweet_id>/
          source-tweet/<source_tweet_id>.json
          reactions/<reply_id_1>.json
          reactions/<reply_id_2>.json
          structure.json
          annotation.json
      non-rumours/
        <source_tweet_id>/
          ...
    ferguson/
      ...

Usage:
  python pheme_to_interactions.py /path/to/pheme_root --events charliehebdo ferguson
  python pheme_to_interactions.py /path/to/pheme_root          # all events
"""

import argparse
import csv
import json
import os
from datetime import datetime
from pathlib import Path


TWITTER_TIME_FORMAT = "%a %b %d %H:%M:%S %z %Y"


def parse_timestamp(raw_created_at: str) -> str:
    """Convert Twitter's created_at format to ISO 8601. Falls back to the
    raw string if parsing fails, so a bad timestamp never crashes the run."""
    try:
        dt = datetime.strptime(raw_created_at, TWITTER_TIME_FORMAT)
        return dt.isoformat()
    except (ValueError, TypeError):
        return raw_created_at


def load_tweet_json(path: Path) -> dict:
    """Load a single tweet's JSON file. Returns {} if missing/corrupt so one
    bad file doesn't kill the whole conversion run."""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def infer_interaction_type(tweet_data: dict) -> str:
    """PHEME reactions are mostly replies; a minority are retweets/quotes.
    Twitter's own fields tell us which."""
    if tweet_data.get("retweeted_status") is not None:
        return "retweet"
    if tweet_data.get("quoted_status") is not None:
        return "quote"
    return "reply"


def flatten_structure(structure, parent_id: str, parent_type: str, edges: list):
    """
    Recursively walk structure.json's nested reply tree and flatten it into
    a list of (child_id, parent_id, parent_type) edges.

    structure.json format: {tweet_id: {child_tweet_id: {...nested...}, ...}}

    Some threads represent "no replies" as an empty list [] instead of an
    empty dict {} - guard against that (and any other non-dict value)
    rather than assume every node in the tree is a dict.
    """
    if not isinstance(structure, dict):
        return
    for child_id, children in structure.items():
        edges.append((child_id, parent_id, parent_type))
        if isinstance(children, dict) and children:
            flatten_structure(children, child_id, "reply", edges)


def derive_veracity_label(annotation: dict):
    """
    Derive the true/false/unverified veracity label from PHEME's raw
    annotation.json fields.

    PHEME's annotation.json format is NOT fully consistent across threads:
    some store 'true' as a direct label string ("true"/"false"/"unverified"),
    others store it as a 0/1 flag that must be combined with 'misinformation'
    per PHEME's own decision table. Handle both rather than assume one and
    crash or silently mislabel on the other.

    Flag-combination table (when 'true' is a 0/1 flag):
      misinformation=0, true=0 -> "unverified"
      misinformation=0, true=1 -> "true"
      misinformation=1, true=0 -> "false"
      misinformation=1, true=1 -> invalid/contradictory -> None
      misinformation present, true absent:
        misinformation=0 -> "unverified"
        misinformation=1 -> "false"
      true present, misinformation absent -> None (insufficient info)
      neither present -> None
    """
    has_misinfo = "misinformation" in annotation
    has_true = "true" in annotation

    # Case 1: 'true' is already a direct label string - some PHEME threads
    # are annotated this way. Use it as-is rather than forcing it through
    # the numeric flag logic (which would crash on a non-numeric string).
    if has_true and isinstance(annotation["true"], str) and annotation["true"].lower() in ("true", "false", "unverified"):
        return annotation["true"].lower()

    # Case 2: numeric flag-based format, per PHEME's published conversion logic
    if has_misinfo and has_true:
        try:
            misinfo = int(annotation["misinformation"])
            true_flag = int(annotation["true"])
        except (ValueError, TypeError):
            return None  # unrecognized format for either field
        if misinfo == 0 and true_flag == 0:
            return "unverified"
        if misinfo == 0 and true_flag == 1:
            return "true"
        if misinfo == 1 and true_flag == 0:
            return "false"
        return None  # misinfo == 1 and true_flag == 1: contradictory, PHEME flags this as invalid

    if has_misinfo and not has_true:
        try:
            misinfo = int(annotation["misinformation"])
        except (ValueError, TypeError):
            return None
        return "unverified" if misinfo == 0 else "false"

    return None  # has 'true' without 'misinformation' in an unrecognized format, or neither field present


def process_thread(thread_dir: Path, label: str, event_name: str) -> tuple:
    """
    Process one thread (one source tweet + its reaction tree).
    Returns (list of interaction rows, veracity record or None, diagnostics dict).
    """
    rows = []
    empty_diagnostics = {"reaction_files_on_disk": 0, "structure_edges_found": 0, "edges_matched": 0, "edges_unmatched": 0, "root_key_mismatch": 0}

    structure_path = thread_dir / "structure.json"
    annotation_path = thread_dir / "annotation.json"
    reactions_dir = thread_dir / "reactions"

    # PHEME releases aren't consistent about this folder's name - some use
    # "source-tweet", others "source-tweets". Check both rather than assume,
    # since guessing wrong here silently drops every thread with 0 rows.
    source_dir = thread_dir / "source-tweets"
    if not source_dir.exists():
        source_dir = thread_dir / "source-tweet"

    source_files = list(source_dir.glob("*.json")) if source_dir.exists() else []
    if not source_files:
        print(f"  WARNING: no source tweet found in {thread_dir} - skipping")
        return rows, None, empty_diagnostics
    source_tweet_id = source_files[0].stem
    source_tweet = load_tweet_json(source_files[0])
    source_user = source_tweet.get("user", {})

    # Record the source tweet itself as a node with no parent (parent_id="")
    rows.append({
        "post_id": source_tweet_id,
        "user_id": source_user.get("id_str", source_user.get("id", "")),
        "parent_id": "",
        "parent_type": "root",
        "interaction_type": "source",
        "timestamp": parse_timestamp(source_tweet.get("created_at", "")),
        "event": event_name,
    })

    # Load all reaction tweets into a lookup so we can pull user/timestamp
    # info when we walk the structure tree
    reaction_lookup = {}
    if reactions_dir.exists():
        for reaction_file in reactions_dir.glob("*.json"):
            data = load_tweet_json(reaction_file)
            if data:
                reaction_lookup[reaction_file.stem] = data
    reaction_files_on_disk = len(reaction_lookup)

    # Flatten structure.json into (child, parent, parent_type) edges.
    # structure.json is keyed by the source tweet id at the top level, e.g.
    # {"1001": {"1002": {}, "1003": {...}}} - so we start recursion from
    # its VALUE (the direct-reply dict), not the dict itself, or the
    # source tweet id gets treated as its own child one level down.
    #
    # KNOWN DATA ISSUE: structure.json's top-level key sometimes does not
    # exactly match source_tweet_id (from the source-tweets/*.json filename).
    # This happens when Twitter's 64-bit tweet IDs pass through any float/JS
    # parsing step during dataset creation, which silently loses precision
    # on IDs this large. Requiring exact equality then finds 0 edges for
    # nearly every thread - not because there are no replies, but because
    # the lookup key is wrong. Since each thread has exactly one root,
    # fall back to structure.json's only top-level key when it doesn't
    # match source_tweet_id, and surface the mismatch so it's visible
    # rather than silently "working" on bad data.
    structure = load_tweet_json(structure_path)
    edges = []
    root_key_mismatch = False
    if structure:
        if source_tweet_id in structure:
            direct_replies = structure[source_tweet_id]
        elif len(structure) == 1:
            root_key_mismatch = True
            direct_replies = next(iter(structure.values()))
        else:
            # Multiple top-level keys and none match - can't safely guess
            # which one is the real root, so treat as no structure found.
            direct_replies = {}
        flatten_structure(direct_replies, source_tweet_id, "post", edges)
    structure_edges_found = len(edges)

    unmatched = 0
    for child_id, parent_id, parent_type in edges:
        tweet_data = reaction_lookup.get(child_id)
        if tweet_data is None:
            # structure.json sometimes references the source tweet id itself
            # or a tweet with no downloaded JSON (deleted/protected) - skip it
            unmatched += 1
            continue
        user = tweet_data.get("user", {})
        rows.append({
            "post_id": child_id,
            "user_id": user.get("id_str", user.get("id", "")),
            "parent_id": parent_id,
            "parent_type": parent_type,
            "interaction_type": infer_interaction_type(tweet_data),
            "timestamp": parse_timestamp(tweet_data.get("created_at", "")),
            "event": event_name,
        })

    diagnostics = {
        "reaction_files_on_disk": reaction_files_on_disk,
        "structure_edges_found": structure_edges_found,
        "edges_matched": structure_edges_found - unmatched,
        "edges_unmatched": unmatched,
        "root_key_mismatch": 1 if root_key_mismatch else 0,
    }

    # Veracity ground truth, keyed by source tweet id
    veracity_record = None
    annotation = load_tweet_json(annotation_path)
    if annotation:
        veracity_record = {
            "post_id": source_tweet_id,
            "event": event_name,
            "category": label,  # "rumours" or "non-rumours"
            "veracity": derive_veracity_label(annotation),  # true / false / unverified / None
            "misinformation_flag": annotation.get("misinformation"),
            "true_flag": annotation.get("true"),
            "is_turnaround": annotation.get("is_turnaround"),
        }

    return rows, veracity_record, diagnostics


def convert(pheme_root: str, events: list, out_dir: str):
    pheme_root = Path(pheme_root)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_rows = []
    all_veracity = []

    event_dirs = (
        [pheme_root / e for e in events]
        if events
        else [d for d in pheme_root.iterdir() if d.is_dir()]
    )

    for event_dir in event_dirs:
        if not event_dir.exists():
            print(f"  Skipping {event_dir} (not found)")
            continue
        print(f"Processing event: {event_dir.name}")

        for label in ("rumours", "non-rumours"):
            label_dir = event_dir / label
            if not label_dir.exists():
                continue

            thread_dirs = [d for d in label_dir.iterdir() if d.is_dir()]
            skipped = 0
            event_reaction_files = 0
            event_structure_edges = 0
            event_edges_matched = 0
            event_root_mismatches = 0
            for thread_dir in thread_dirs:
                rows, veracity, diag = process_thread(thread_dir, label, event_dir.name)
                if not rows:
                    skipped += 1
                all_rows.extend(rows)
                if veracity:
                    all_veracity.append(veracity)
                event_reaction_files += diag["reaction_files_on_disk"]
                event_structure_edges += diag["structure_edges_found"]
                event_edges_matched += diag["edges_matched"]
                event_root_mismatches += diag["root_key_mismatch"]

            status = f"  {label}: {len(thread_dirs)} threads"
            if skipped:
                status += f" ({skipped} skipped - see WARNINGs above)"
            print(status)
            print(f"    reaction files on disk: {event_reaction_files}, "
                  f"structure.json edges: {event_structure_edges}, "
                  f"matched: {event_edges_matched}")
            if event_root_mismatches:
                print(f"    NOTE: {event_root_mismatches}/{len(thread_dirs)} threads had a "
                      f"structure.json root key that didn't match the source tweet id exactly "
                      f"(used fallback) - likely ID precision loss upstream in this dataset release")
            if event_structure_edges > 0 and event_edges_matched < event_structure_edges * 0.9:
                print(f"    WARNING: {event_structure_edges - event_edges_matched} structure edges "
                      f"did not match a reaction file - check reaction file naming/id format")

    # Write interactions.csv
    interactions_path = out_dir / "interactions.csv"
    fieldnames = ["post_id", "user_id", "parent_id", "parent_type", "interaction_type", "timestamp", "event"]
    with open(interactions_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    # Write veracity_labels.json
    veracity_path = out_dir / "veracity_labels.json"
    with open(veracity_path, "w", encoding="utf-8") as f:
        json.dump(all_veracity, f, indent=2)

    print(f"\nDone.")
    print(f"  interactions.csv    : {len(all_rows)} rows -> {interactions_path}")
    print(f"  veracity_labels.json: {len(all_veracity)} threads -> {veracity_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert PHEME dataset to interactions.csv")
    parser.add_argument("pheme_root", help="Path to the unzipped PHEME dataset root folder")
    parser.add_argument("--events", nargs="*", default=None,
                         help="Specific event folder names to process (default: all events found)")
    parser.add_argument("--out-dir", default=".", help="Output directory (default: current directory)")
    args = parser.parse_args()

    convert(args.pheme_root, args.events, args.out_dir)