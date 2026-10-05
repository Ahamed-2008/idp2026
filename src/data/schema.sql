-- ============================================================================
-- Q-MisinfoGuard: PHEME Misinformation Propagation SQLite Database Schema
-- File: src/data/schema.sql
-- ============================================================================
-- Notes:
--   1. PRAGMA foreign_keys = ON must be executed on every connection.
--   2. structure.json is the single source of truth for parent/child tree edges.
--   3. followers_count, retweet_count, and favorite_count are crawl-time snapshots,
--      not dynamic features that reflect cascade evolution over time.
--   4. Twitter Snowflake 64-bit ID timestamp calculation formula:
--      created_ms = (tweet_id >> 22) + 1288834974657
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ----------------------------------------------------------------------------
-- 1. THREADS
-- Stores ground truth claim annotations, veracity labels, and story metadata
-- for each conversational thread / cascade.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS threads (
    thread_id           TEXT PRIMARY KEY,              -- Thread folder name (root tweet ID)
    source_tweet_id     TEXT,                          -- Root key from structure.json (retains ID precision)
    event               TEXT NOT NULL,                 -- Event name (e.g., 'charliehebdo', 'ferguson')
    is_rumour           TEXT NOT NULL,                 -- 'rumour' or 'non-rumour'
    category            TEXT,                          -- Claim story headline (e.g., 'Mike Brown was shot 10 times')
    veracity            TEXT,                          -- 'true', 'false', 'unverified', or NULL (non-rumours & contradictions)
    misinformation      INTEGER,                       -- Raw flag: 0 (verified/unverified), 1 (false)
    true_flag           TEXT,                          -- Raw flag from annotation: 0, 1, or string label
    is_turnaround       INTEGER                        -- 1 if rumour was debunked/turned around during cascade, else 0
);

-- ----------------------------------------------------------------------------
-- 2. TWEETS
-- Stores all source and reaction tweets with user profile details, text, and
-- engagement snapshots.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tweets (
    thread_id           TEXT NOT NULL,                 -- Foreign key to threads(thread_id)
    tweet_id            TEXT NOT NULL,                 -- Tweet ID string
    user_id             TEXT,                          -- Author user ID string
    screen_name         TEXT,                          -- Author screen name / handle
    followers_count     INTEGER,                       -- Author followers (crawl-time snapshot)
    verified            INTEGER,                       -- 1 if user verified, 0 otherwise
    text                TEXT,                          -- Complete tweet text
    created_at          TEXT,                          -- ISO 8601 formatted timestamp
    created_ms          INTEGER NOT NULL,              -- Epoch timestamp in milliseconds (with Snowflake fallback)
    retweet_count       INTEGER,                       -- Retweet count (crawl-time snapshot)
    favorite_count      INTEGER,                       -- Favorite / like count (crawl-time snapshot)
    is_source           INTEGER NOT NULL,              -- 1 if root/source tweet of thread, 0 if reaction/reply
    in_reply_to_tweet_id TEXT,                         -- Raw Twitter metadata field (not used for tree logic)
    in_reply_to_user_id TEXT,                          -- Raw Twitter metadata field
    raw_json            TEXT,                          -- Complete raw tweet JSON payload (optional)
    PRIMARY KEY (thread_id, tweet_id),
    FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
);

-- ----------------------------------------------------------------------------
-- 3. INTERACTIONS (Propagation Tree Edges)
-- Derived strictly from structure.json. Represents parent -> child diffusion edges.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS interactions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id           TEXT NOT NULL,                 -- Foreign key to threads(thread_id)
    parent_id           TEXT NOT NULL,                 -- Parent tweet ID
    child_id            TEXT NOT NULL,                 -- Child tweet ID (reply, retweet, or quote)
    parent_type         TEXT,                          -- 'post' (if parent is source_tweet_id) or 'reply'
    interaction_type    TEXT NOT NULL,                 -- 'reply', 'retweet', or 'quote'
    depth               INTEGER NOT NULL,              -- Tree depth (1 for direct replies, 2 for nested, etc.)
    created_ms          INTEGER,                       -- Milliseconds timestamp of child tweet
    timestamp           TEXT,                          -- ISO 8601 timestamp of child tweet
    UNIQUE(thread_id, child_id),
    FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
);

-- ----------------------------------------------------------------------------
-- 4. EVIDENCE_LINKS
-- External fact-checking URLs, journalistic sources, and stance classifications.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence_links (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id           TEXT NOT NULL,                 -- Foreign key to threads(thread_id)
    url                 TEXT,                          -- Fact-checking URL
    mediatype           TEXT,                          -- Media type (e.g., 'news-media')
    position            TEXT,                          -- 'for', 'against', or 'observing'
    FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
);

-- ----------------------------------------------------------------------------
-- PERFORMANCE INDEXES
-- ----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_threads_event_veracity ON threads(event, veracity);
CREATE INDEX IF NOT EXISTS idx_tweets_user_id ON tweets(user_id);
CREATE INDEX IF NOT EXISTS idx_tweets_thread ON tweets(thread_id);
CREATE INDEX IF NOT EXISTS idx_interactions_thread ON interactions(thread_id);
CREATE INDEX IF NOT EXISTS idx_interactions_parent ON interactions(parent_id);
CREATE INDEX IF NOT EXISTS idx_interactions_child ON interactions(child_id);
CREATE INDEX IF NOT EXISTS idx_evidence_links_thread ON evidence_links(thread_id);

-- ----------------------------------------------------------------------------
-- HELPER VIEWS
-- ----------------------------------------------------------------------------

-- View: Complete propagation edge view with child tweet content (LEFT JOIN handles deleted tweets)
CREATE VIEW IF NOT EXISTS v_propagation_edges AS
SELECT 
    i.id AS edge_id,
    i.thread_id,
    th.event,
    th.is_rumour,
    th.veracity,
    i.parent_id,
    i.child_id,
    i.parent_type,
    i.interaction_type,
    i.depth,
    i.created_ms,
    i.timestamp,
    t.user_id AS child_user_id,
    t.screen_name AS child_screen_name,
    t.text AS child_text,
    t.followers_count AS child_user_followers
FROM interactions i
JOIN threads th ON i.thread_id = th.thread_id
LEFT JOIN tweets t ON i.thread_id = t.thread_id AND i.child_id = t.tweet_id;

-- View: Cascade aggregate metrics per thread
CREATE VIEW IF NOT EXISTS v_cascade_summary AS
SELECT 
    th.thread_id,
    th.event,
    th.is_rumour,
    th.veracity,
    th.category,
    COUNT(DISTINCT i.child_id) AS total_reactions,
    COALESCE(MAX(i.depth), 0) AS max_depth,
    MIN(i.created_ms) AS first_reaction_ms,
    MAX(i.created_ms) AS last_reaction_ms,
    CASE 
        WHEN COUNT(i.child_id) > 0 THEN (MAX(i.created_ms) - MIN(i.created_ms)) / 1000.0
        ELSE 0.0 
    END AS duration_seconds
FROM threads th
LEFT JOIN interactions i ON th.thread_id = i.thread_id
GROUP BY th.thread_id;
