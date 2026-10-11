# Graph layout spike (E7.1), 2026-10-11

Question: which layout should place the backbone views, Fruchterman-Reingold (FR, igraph, what the views used
before) or ForceAtlas2 (FA2, LinLog attraction, numpy implementation in `graph/layout.py`), each followed by the
overlap pass, and laid out on all edges or only on the disparity-filtered (`bb`) edges?

**Decision: FA2 on the `bb` edges** (`layout.ALGORITHM = "fa2"`, views pass only kept edges to the layout).

Method: the 300-entity backbone of 2025-10, 2026-08 (the largest month) and 2026-09, for XRP and all assets.
Every run ends with zero overlapping nodes, so the comparison is about how much space the layout gives the
nodes and how much the overlap pass had to move them. "Most nodes within 60 px" is the crowding of the
busiest spot on the 1,000 x 700 reference stage (lower is better); "median shift" is how far the overlap pass
moved a node (lower = the layout was already clean).

| Month | Asset | Layout | Edges laid out | Layout s | Overlap pass s | Median shift px | Median nearest neighbour px | Most nodes within 60 px |
|---|---|---|---|---|---|---|---|---|
| 2025-10 | XRP | FR | all (3,248) | 0.26 | 3.25 | 24.3 | 10.7 | 85 |
| 2025-10 | XRP | FR | bb (485) | 0.27 | 2.13 | 9.0 | 10.8 | 71 |
| 2025-10 | XRP | FA2 | all (3,248) | 2.36 | 0.82 | 2.6 | 13.1 | 55 |
| 2025-10 | XRP | FA2 | bb (485) | 1.95 | 0.61 | 8.3 | 11.2 | 27 |
| 2025-10 | all | FR | all (6,784) | 0.18 | 1.72 | 13.3 | 12.6 | 81 |
| 2025-10 | all | FR | bb (811) | 0.26 | 3.44 | 15.7 | 11.5 | 77 |
| 2025-10 | all | FA2 | all (6,784) | 2.82 | 0.48 | 2.4 | 14.1 | 45 |
| 2025-10 | all | FA2 | bb (811) | 2.04 | 0.14 | 2.5 | 12.5 | 38 |
| 2026-08 | XRP | FR | all (3,690) | 0.27 | 3.5 | 28.4 | 10.7 | 92 |
| 2026-08 | XRP | FR | bb (535) | 0.25 | 2.58 | 12.3 | 10.9 | 73 |
| 2026-08 | XRP | FA2 | all (3,690) | 2.47 | 0.22 | 1.8 | 13.3 | 32 |
| 2026-08 | XRP | FA2 | bb (535) | 1.98 | 0.48 | 7.0 | 11.6 | 33 |
| 2026-08 | all | FR | all (6,910) | 0.28 | 2.71 | 25.5 | 11.1 | 91 |
| 2026-08 | all | FR | bb (721) | 0.25 | 1.27 | 2.8 | 12.4 | 72 |
| 2026-08 | all | FA2 | all (6,910) | 2.88 | 0.59 | 0.3 | 12.8 | 78 |
| 2026-08 | all | FA2 | bb (721) | 2.06 | 0.51 | 10.4 | 12.5 | 35 |
| 2026-09 | XRP | FR | all (3,186) | 0.26 | 3.45 | 45.7 | 11.2 | 92 |
| 2026-09 | XRP | FR | bb (495) | 0.25 | 2.18 | 8.1 | 11.5 | 68 |
| 2026-09 | XRP | FA2 | all (3,186) | 2.35 | 0.49 | 4.5 | 13.1 | 39 |
| 2026-09 | XRP | FA2 | bb (495) | 1.99 | 0.54 | 9.6 | 12.5 | 31 |
| 2026-09 | all | FR | all (6,713) | 0.28 | 2.88 | 26.0 | 10.1 | 110 |
| 2026-09 | all | FR | bb (712) | 0.25 | 0.98 | 8.9 | 11.0 | 92 |
| 2026-09 | all | FA2 | all (6,713) | 2.84 | 0.68 | 1.2 | 11.3 | 78 |
| 2026-09 | all | FA2 | bb (712) | 2.02 | 5.24 | 17.9 | 11.0 | 94 |

Findings:

- FA2 spreads the nodes: the busiest 60 px spot holds 27 to 45 nodes on the filtered XRP backbones against
  68 to 92 for FR. The overlap pass barely moves FA2 layouts on all edges (0.3 to 4.5 px median).
- Laying out only the `bb` edges lowers crowding further for FA2 on XRP and stops weak edges pulling unrelated
  hubs together. The all-assets 2026-09 backbone is the exception (94); it has one very large fan.
- FA2 costs about 2 to 3 s per 300-node view against 0.3 s for FR; the overlap pass is 0.1 to 5 s. Ego views
  (about 51 nodes) take 0.07 s. A full month's views step went from 407 s to about 350 s, because sample
  transaction hashes are now looked up only for drawn edges.
- Before/after on 2026-09 XRP (`graph/preview.py` checks at the reference stage): 1,414 overlapping node pairs
  and 7,378 label collisions before; 0 overlaps and 13 label collisions (30 labels shown) after.

Follow-ups found while building:

- Small disconnected pairs far from the main cluster stretched the bounding box. FA2 gravity keeps them close.
- The plain disparity filter cut the weak bridges between clusters, so pieces drifted apart. The filter now always
  keeps a maximum spanning forest (`backbone.spanning_forest`).
- Picking a community's 150 most connected members gave isolated bot pairs. Bundles now grow a connected group
  from the community's top entity along its strongest ties (`views.grow`).
