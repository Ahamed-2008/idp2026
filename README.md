# Q-MisinfoGuard

Misinformation detection, propagation modeling, and intervention selection,
combining NLP classification, GNN-based spread prediction, and a classical/
quantum optimization comparison for choosing where to intervene.

## Project structure

```
q-misinfoguard/
├── data/
│   ├── raw/            Unmodified PHEME download (gitignored, ~300MB)
│   ├── processed/       interactions.csv, veracity_labels.json per event
│   └── external/        detection_output.json from the detection model
├── src/
│   ├── detection/        NLP misinformation classifier (teammate's module)
│   ├── graph/             pheme_to_interactions.py, build_propagation_graph.py
│   ├── gnn/               spread prediction model (Phase 2)
│   ├── optimization/      classical + QUBO/QAOA intervention selection (Phase 2-3)
│   └── dashboard/         demo UI (Phase 3)
├── notebooks/             exploration and validation, not pipeline code
├── outputs/
│   ├── graphs/            .graphml (inspection) + .pkl (pipeline) per event
│   └── models/            trained model checkpoints
├── tests/                 unit tests, e.g. structure.json edge cases
└── docs/                  proposal PDF, phase notes, final report
```

## Phase 1 - data pipeline, detection model, graph construction
1. Download PHEME (9-event) into `data/raw/` and build `data/processed/pheme.db` (see [docs/DATA_SETUP.md](docs/DATA_SETUP.md)).
2. `python src/graph/pheme_to_interactions.py data/raw/<event> --out-dir data/processed/<event>`
3. Get `detection_output.json` from the detection module, place in `data/external/`.
4. `python src/graph/build_propagation_graph.py` (update its hardcoded paths, or pass as args)
5. Validate: check `outputs/graphs/` for sane node/edge counts before moving on.

## Phase 2 - spread prediction & classical optimization
GNN training against `outputs/graphs/*.pkl`, evaluated on `veracity_labels.json`.
Classical intervention selection baseline (greedy / centrality / simulated annealing).

## Phase 3 - quantum optimization, integration, reporting
QUBO formulation benchmarked against the Phase 2 classical baseline. Dashboard
wiring the full pipeline together for demo purposes.

## Setup
```bash
pip install -r requirements.txt
```
