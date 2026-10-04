"""
Q-MisinfoGuard: Social Graph Construction Module (Module 5.3)

Builds one propagation graph per misinformation cascade, merging:
  - Detection model output (risk score, label) -> attached to the post node
  - Interaction data (who replied/retweeted whom) -> defines edges

Expected inputs:
  1. detection_output.json
     [{"post_id": "1234", "risk_score": 0.87, "label": "suspicious"}, ...]

  2. interactions.csv  (one row per reply/retweet)
     post_id, user_id, parent_id, parent_type, interaction_type, timestamp
     - post_id:       the id of THIS post/reply/retweet
     - parent_id:     the id of the post/user it responded to (source post if
                       this is a direct reply, or another reply if nested)
     - parent_type:   "post" or "reply"
     - interaction_type: "reply", "retweet", "quote", "mention"
     - timestamp:     ISO 8601 or unix time

Output:
  A NetworkX DiGraph, saved as GraphML (for inspection) and pickle
  (for the next module, spread-risk prediction, to load directly).
"""

import json
import csv
import pickle
import networkx as nx


def load_detection_scores(json_path: str) -> dict:
    """Load risk scores keyed by post_id for fast lookup."""
    with open(json_path, "r") as f:
        records = json.load(f)
    return {r["post_id"]: r for r in records}


def build_cascade_graph(interactions_csv: str, detection_scores: dict) -> nx.DiGraph:
    """
    Build a directed graph for one or more cascades.
    Edge direction: parent -> child (information flows from the
    original/parent post to the person who reshared/replied).
    """
    G = nx.DiGraph()

    with open(interactions_csv, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            post_id = row["post_id"]
            parent_id = row["parent_id"]
            user_id = row["user_id"]

            # Add the post node, attaching the detection model's risk score
            # if this post was scored (source posts usually are; replies
            # may or may not be, depending on your teammate's pipeline).
            score_info = detection_scores.get(post_id, {})
            G.add_node(
                post_id,
                node_type="post",
                user_id=user_id,
                risk_score=score_info.get("risk_score"),
                label=score_info.get("label"),
                timestamp=row["timestamp"],
            )

            # Source tweet rows (from pheme_to_interactions.py) have no
            # parent - they ARE the root of the cascade. Add the node but
            # skip creating an edge, since there's no real parent to link to.
            if not parent_id:
                continue

            # Add the parent node if it isn't already in the graph
            if parent_id not in G:
                parent_score_info = detection_scores.get(parent_id, {})
                G.add_node(
                    parent_id,
                    node_type=row["parent_type"],
                    risk_score=parent_score_info.get("risk_score"),
                    label=parent_score_info.get("label"),
                )

            # Edge: information flows from parent to this post
            G.add_edge(
                parent_id,
                post_id,
                interaction_type=row["interaction_type"],
                timestamp=row["timestamp"],
            )

    return G


def add_graph_features(G: nx.DiGraph) -> nx.DiGraph:
    """
    Pass 3: compute centrality and structural features, PER CASCADE.

    IMPORTANT: this must be done per weakly-connected component, not on the
    whole graph at once. degree_centrality and betweenness_centrality
    normalize by the total node count of the graph passed in - if you hand
    them a graph merging thousands of unrelated cascades, every node's
    score gets normalized against thousands of nodes it has no path to,
    making the values near-zero and useless for comparing importance
    WITHIN a cascade (which is what the spread-prediction module needs).
    Computing per-component fixes this and is also faster in practice.

    Run this only after you've confirmed the graph structure (Pass 1)
    and node attributes (Pass 2) look correct.
    """
    components = list(nx.weakly_connected_components(G))
    print(f"Computing centrality across {len(components)} cascades...")

    for component_nodes in components:
        subgraph = G.subgraph(component_nodes)

        # Single-node cascades (a source tweet with 0 matched replies) have
        # no meaningful centrality - default to 0 rather than let networkx
        # error or return a degenerate value.
        if len(component_nodes) == 1:
            node = next(iter(component_nodes))
            G.nodes[node]["degree_centrality"] = 0.0
            G.nodes[node]["betweenness_centrality"] = 0.0
            G.nodes[node]["pagerank"] = 1.0
            continue

        degree_centrality = nx.degree_centrality(subgraph)
        betweenness = nx.betweenness_centrality(subgraph)
        pagerank = nx.pagerank(subgraph)

        for node in component_nodes:
            G.nodes[node]["degree_centrality"] = degree_centrality[node]
            G.nodes[node]["betweenness_centrality"] = betweenness[node]
            G.nodes[node]["pagerank"] = pagerank[node]

    return G


def save_graph(G: nx.DiGraph, out_prefix: str):
    """Save in two formats: GraphML for manual inspection, pickle for
    the spread-risk prediction module to load directly (preserves all
    Python-native attribute types, which GraphML sometimes flattens).

    GraphML has no representation for None (e.g. risk_score/label on
    nodes the detection model didn't score), so we sanitize a COPY for
    that export only. The pickle keeps real None values intact, since
    downstream modules should be able to tell "unscored" apart from an
    actual score."""
    G_graphml_safe = G.copy()
    for _, data in G_graphml_safe.nodes(data=True):
        for key, value in data.items():
            if value is None:
                data[key] = ""
    for _, _, data in G_graphml_safe.edges(data=True):
        for key, value in data.items():
            if value is None:
                data[key] = ""

    nx.write_graphml(G_graphml_safe, f"{out_prefix}.graphml")
    with open(f"{out_prefix}.pkl", "wb") as f:
        pickle.dump(G, f)


def summarize(G: nx.DiGraph):
    """Quick sanity check before moving to the next module."""
    print(f"Nodes: {G.number_of_nodes()}")
    print(f"Edges: {G.number_of_edges()}")
    print(f"Is weakly connected: {nx.is_weakly_connected(G)}")
    scored_nodes = [n for n, d in G.nodes(data=True) if d.get("risk_score") is not None]
    print(f"Nodes with a risk score attached: {len(scored_nodes)}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build a propagation graph from interactions.csv + detection scores")
    parser.add_argument("interactions_csv", help="Path to interactions.csv (from pheme_to_interactions.py)")
    parser.add_argument("--detection-json", default="detection_output.json",
                         help="Path to detection model's JSON output (default: detection_output.json)")
    parser.add_argument("--out-prefix", default="cascade_graph",
                         help="Output filename prefix for .graphml/.pkl (default: cascade_graph)")
    args = parser.parse_args()

    scores = load_detection_scores(args.detection_json)
    G = build_cascade_graph(args.interactions_csv, scores)
    G = add_graph_features(G)
    summarize(G)
    save_graph(G, args.out_prefix)