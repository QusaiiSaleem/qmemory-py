# Graph Visualization for `/memories/graph` — Knowledge Backbone View

**Date:** 2026-04-11
**Status:** Approved
**Scope:** Add a new `/memories/graph` page that renders the memory graph as an interactive visualization, focused on the structural backbone (hubs, clusters, and meaningful edges).

## Motivation

The current `/memories` page shows a flat list with search and category filters. Users can click a memory to see a text list of linked nodes on `/memories/{id}`, but there is no visual understanding of:

1. **Which memories are hubs** (high-connectivity anchors of the knowledge graph)
2. **How clusters relate to each other** (cross-topic connections)
3. **Where the orphans and underconnected areas are**
4. **The difference between the rich knowledge backbone (~166 structural nodes) and the bulk book-chunk layer (~8,575 chunks)**

A product designer working with their own memory system needs to see the system's architecture, not just browse entries. This page gives them that view.

### Scale Reality (verified against production)

| Threshold | Node count |
|-----------|------------|
| degree ≥ 1 (any edge) | 8,741 |
| **degree ≥ 2** | **166** ← structural backbone |
| degree ≥ 3 | 138 |
| degree ≥ 5 | 94 |
| degree ≥ 10 | 71 |
| degree ≥ 20 | 55 |

99% of the 8,741 linked nodes are book chunks (each with exactly 1 edge to their book entity). The **meaningful knowledge architecture lives in the 166 backbone nodes**: NCNP HUB, Cultural HUB, Rakeezah Roadmap, Dr. Magdy cluster, EduArabia cluster, Strategy House, etc. The top 16 hubs are all book entities (545 chunks max).

**Design consequence:** default view = backbone filter (degree ≥ 2) + collapse books. Showing all 8,741 nodes would drown the structural signal in book-chunk noise.

## Architecture

### Component Overview

```
┌──────────────── Browser ──────────────────┐
│  /memories/graph  ──► page.html           │
│       ▼ JS IIFE                            │
│  Cytoscape 3.29 (CDN)                     │
│  ├─► GET /memories/graph.json (on mount)  │
│  │      ↓                                  │
│  │   {nodes, edges, stats}                 │
│  └─► on click → htmx.ajax(                 │
│         GET /memories/graph/node/{id}      │
│         target: #detail-panel              │
│      )                                     │
└────────────────────────────────────────────┘
               │                  │
               ▼                  ▼
┌──────────────────────────────────────────────┐
│  Backend: qmemory/                           │
│  core/graph_viz.py                           │
│    • get_backbone(min_degree, include_books) │
│    • get_ego(node_id, hops)                  │
│    • get_book_chunks(book_id, limit, offset) │
│                                               │
│  app/routes/graph.py                         │
│    • GET /memories/graph                     │
│    • GET /memories/graph.json                │
│    • GET /memories/graph/node/{id}           │
└──────────────────────────────────────────────┘
               │
               ▼
         SurrealDB
  (relates table + memory + entity)
```

### Pattern Source

This design adapts the verified pattern from the user's own `nonprofit-knowledge-hub/templates/pages/graph.html`:

- Cytoscape.js 3.29 from CDN (no npm, no build step — matches `#NoBuild`)
- `cose` force-directed layout with tuned parameters (nodeRepulsion 14000, idealEdgeLength 140, gravity 0.3)
- IIFE initialization (not `DOMContentLoaded`) for `hx-boost` compatibility
- Split pane: graph on flex-1, detail panel on right (w-[420px])
- `htmx.ajax('GET', '/memories/graph/node/{id}', ...)` on node click
- Alpine.js for toolbar reactive state (activeFilter, searchQuery)

The visual theme diverges: nonprofit-knowledge-hub uses DaisyUI beige/light, qmemory uses the existing `base.html` cyberpunk dark theme (`#050505` bg, `#00ffaa` primary, Cairo/Space Grotesk fonts, dot-matrix background, scan-line overlay).

## Data Model

### Node JSON shape

```json
{
  "id": "memory:mem1775911426513pli",
  "type": "memory",
  "subtype": "domain",
  "label": "NCNP 2024 HUB",
  "content_preview": "⚠️ سري — غير منشور. الاستراتيجية الوطنية...",
  "degree": 29,
  "salience": 0.95,
  "is_collapsed_book": false,
  "chunk_count": null
}
```

Field meanings:

- `type`: `"memory"` or `"entity"` — table name of the node
- `subtype`: memory category (one of 8) OR entity type (`person`/`book`/`topic`)
- `label`: first 40 characters of content (for memories) or entity name (truncated)
- `content_preview`: first 120 characters, shown on hover tooltip
- `degree`: total edges (in + out) — drives node size
- `salience`: 0.0-1.0, used as a secondary tiebreaker for size
- `is_collapsed_book`: `true` for the 16 book entities that are rendered as diamond mega-nodes (hiding their chunk children)
- `chunk_count`: integer number of hidden chunks (shown on hover) when `is_collapsed_book` is true

### Edge JSON shape

```json
{
  "id": "relates:fga6ysuezg616b8jqk6f",
  "source": "memory:mem1775033662238pbs",
  "target": "memory:mem1775911810577apm",
  "relationship_type": "feature_aligned_with_sector_enablers"
}
```

Only edges whose `source` AND `target` are both in the visible node set are returned. `relationship_type` may be `null` for the legacy 8,675 bulk-imported edges — those are rendered with neutral styling and no hover label.

### Stats JSON shape

```json
{
  "total_nodes": 132,
  "total_edges": 240,
  "hubs_count": 71,
  "books_collapsed": 16,
  "hidden_chunks": 8609
}
```

`hubs_count` = nodes with degree ≥ 10 (71 in current production). `total_nodes` in the example assumes default filter (degree ≥ 2, books included, 16 collapsed diamonds + 116 other nodes ≈ 132). These are illustrative; the actual numbers come from the live query. Rendered in the page header next to the title.

## Backend

### New file: `qmemory/core/graph_viz.py`

Estimated 180 lines. Three async functions, all following the `async with get_db() as db:` pattern used across the codebase.

```python
async def get_backbone(
    db,
    min_degree: int = 2,
    include_books: bool = True,
    include_categories: list[str] | None = None,
) -> dict:
    """Build backbone graph for frontend rendering.

    Steps:
      1. Compute degree per node via one query over `relates` (SELECT in, out FROM relates).
      2. Filter to nodes with degree ≥ min_degree. Separate memory vs entity.
      3. Identify book entities — query entity WHERE type='book'. For each book,
         count its from_book chunks (hidden from the node set), mark
         is_collapsed_book=true, and store chunk_count.
      4. Build the visible node set (memories + non-book entities + collapsed books).
      5. Query memories/entities for the visible set to get content, category,
         name, salience (use MEMORY_FIELDS constant — no SELECT *).
      6. Fetch edges WHERE in IN $visible AND out IN $visible.
      7. Return {nodes, edges, stats}.
    """

async def get_ego(db, node_id: str, hops: int = 1) -> dict:
    """Fetch one node + its immediate neighbors.

    Used by the detail panel when a user clicks a non-book node.
    Reuses `qmemory/core/get.py::get_memories` with include_neighbors=true,
    which already implements the ego traversal via `_attach_neighbors`.
    Returns the same response shape (memories list with neighbors nested).
    """

async def get_book_chunks(
    db, book_entity_id: str, limit: int = 20, offset: int = 0,
) -> dict:
    """Paginated chunk list for the detail panel when a collapsed book is clicked.

    Traverses book entity -> from_book <- memory chunks, returns the chunks
    sorted by created_at. Includes has_more for pagination.
    """
```

**Performance notes:**

- `get_backbone` is the hot path. The fix is to avoid the `WHERE in IN (...)` anti-pattern documented in CLAUDE.md (191 second scan). Instead, traverse `SELECT in, out FROM relates` once into Python, compute degree in Python, then do a targeted `SELECT ... WHERE id INSIDE $ids` only for the filtered visible set (~150 IDs).
- Expected latency: < 500ms for 166-node backbone. Profile after slice 1 lands.
- No caching in v1 — recompute every request. Cache can come later if needed.

### New file: `qmemory/app/routes/graph.py`

Estimated 80 lines. Three routes, all gated on `get_session_user(request)` like `routes/memories.py`.

```python
@router.get("/memories/graph", response_class=HTMLResponse)
async def graph_page(request: Request):
    """Render the empty page shell. Data loads via HTMX on mount."""

@router.get("/memories/graph.json", response_class=JSONResponse)
async def graph_data(
    request: Request,
    min_degree: int = 2,
    books: int = 1,
    cats: str = "",
):
    """Return the backbone graph JSON.

    Query params:
      min_degree: int, default 2
      books: 0 or 1 (include collapsed book mega-nodes)
      cats: comma-separated category list to include, empty = all
    """

@router.get("/memories/graph/node/{node_id}", response_class=HTMLResponse)
async def graph_node_detail(request: Request, node_id: str):
    """Return the detail panel HTMX partial.

    Branches on node type:
      - memory → render partials/graph_node_memory.html (content + neighbors)
      - entity (person/topic) → render partials/graph_node_entity.html (name + linked mems)
      - entity (book, is_collapsed) → render partials/graph_node_book.html (chunks + pagination)
    """
```

The route module is wired into `qmemory/app/main.py` alongside the existing `memories` router.

### New partials

Three HTMX partials under `qmemory/app/templates/partials/`:

1. `graph_node_memory.html` — single memory with full content, category badge, salience bar, and a list of ego neighbors (reusing `memory_card.html` styling).
2. `graph_node_entity.html` — entity name/type, aliases if present, and a list of linked memories.
3. `graph_node_book.html` — book name, total chunk count, paginated chunk list with "load more" (HTMX `hx-get` for next page).

All three follow the existing dark cyberpunk style from `base.html`.

## Frontend

### New file: `qmemory/app/templates/pages/graph.html`

Estimated 450 lines total (200 HTML, 250 JS). Extends `base.html`, matching the existing page style.

### Layout

```
┌─────────────────────────────────────────────────────────┐
│  [nav bar — base.html]                                  │
├─────────────────────────────────────────────────────────┤
│  🧠 Memory Graph              132 nodes · 240 edges    │
├─────────────────────────────────────────────────────────┤
│  [🔎 Search...] [all][self][idea][...] [☐books] [↻][+−]│
├────────────────────────────────┬────────────────────────┤
│  #cy (flex-1)                  │  #detail-panel         │
│                                │  (w-[420px], hidden     │
│         ○                      │   until click)          │
│        ╱│╲                     │                         │
│   ○───⚫───○     ●              │                         │
│        │     ╱│╲               │                         │
│        ○    ○ ◇ ○              │                         │
│                book             │                         │
├────────────────────────────────┴────────────────────────┤
│  Legend: 🟢 self  🟣 pref  🔵 context  📕 book ...     │
└─────────────────────────────────────────────────────────┘
```

### Cytoscape Configuration

```javascript
var COLORS = {
  memory: {
    self: '#00ffaa', style: '#ff6b9d', preference: '#a855f7',
    context: '#3b82f6', decision: '#f59e0b', idea: '#10b981',
    feedback: '#ef4444', domain: '#6b7280',
  },
  entity: {
    person: '#ff6b9d', book: '#a855f7', topic: '#3b82f6',
  },
};

style: [
  { selector: 'node', style: {
    'background-color': (ele) => getColor(ele),
    'width': (ele) => 6 + Math.log(ele.data('degree') + 1) * 8,
    'height': (ele) => 6 + Math.log(ele.data('degree') + 1) * 8,
    'shape': (ele) =>
        ele.data('is_collapsed_book') ? 'diamond'
      : ele.data('type') === 'entity' ? 'roundrectangle'
      : 'ellipse',
    'label': (ele) => ele.data('degree') >= 5 ? ele.data('label') : '',
    'font-family': 'Cairo, Space Grotesk, sans-serif',
    'font-size': '9px',
    'color': '#e0e0e0',
    'text-outline-color': '#050505',
    'text-outline-width': 2,
    'border-width': (ele) => ele.data('degree') >= 10 ? 2 : 0,
    'border-color': '#00ffaa',
  }},
  { selector: 'edge', style: {
    'line-color': '#2a2a2a',
    'width': 1,
    'opacity': 0.4,
    'curve-style': 'bezier',
    'target-arrow-shape': 'triangle',
    'target-arrow-color': '#2a2a2a',
  }},
  { selector: 'edge:selected', style: {
    'line-color': '#00ffaa',
    'width': 2,
    'opacity': 1,
    'label': 'data(relationship_type)',
    'font-size': '9px',
    'color': '#00ffaa',
  }},
  { selector: '.dimmed', style: { 'opacity': 0.08 }},
  { selector: '.highlighted', style: {
    'border-width': 3, 'border-color': '#00ffaa' }},
],
layout: {
  name: 'cose',
  nodeRepulsion: 14000,
  idealEdgeLength: 140,
  gravity: 0.3,
  randomize: true,
  padding: 40,
  animate: true,
  animationDuration: 800,
},
minZoom: 0.2,
maxZoom: 4,
wheelSensitivity: 0.3,
```

### Toolbar (Alpine.js state)

```html
<div x-data="{
  minDegree: 2,
  showBooks: false,
  activeCategory: 'all',
  searchQuery: ''
}">
```

Reactive handlers: `filterBySearch`, `filterByCategory`, `toggleBooks`, `changeDegree`, `zoomIn`, `zoomOut`, `refreshGraph`. Exposed on `window` so Alpine can call them (same pattern as nonprofit-knowledge-hub).

## Interactions

| Event | Behavior |
|-------|----------|
| Click memory/entity node | `htmx.ajax` to `/memories/graph/node/{id}`, target `#detail-panel`, show panel |
| Click collapsed book diamond | Same endpoint, server branches on node type → returns book detail partial with paginated chunks |
| Click edge | Cytoscape `select()` on edge → reveals `relationship_type` label via `:selected` style |
| Hover edge | Temporary select → label appears, disappears on mouseout |
| Click background | Hide `#detail-panel` |
| Search input | Debounce 200ms → dim non-matching nodes (`label.contains(query)`) |
| Category filter button | Active button highlighted, non-matching nodes dimmed |
| Books toggle | Re-fetch `/memories/graph.json?books=0` or `books=1`, re-init Cytoscape |
| Degree slider | Re-fetch with new `min_degree`, re-init Cytoscape |
| `+` / `−` buttons | `cy.zoom(cy.zoom() * 1.3)` / `* 0.7`, then `cy.center()` |
| `↻` refresh | `location.reload()` (simplest, matches nonprofit pattern) |

## Testing

Manual verification only — no new pytest suite. This is a read-only visualization page; the test cost exceeds the value for UI code in this stack.

**Slice 1 (backend) smoke test:**
```bash
curl -s http://localhost:3777/memories/graph.json \
  -H "Cookie: session=..." | jq '.stats'
# Expected: {"total_nodes": ~132, "total_edges": ~240, ...}
```

**Slice 3+ (frontend) manual test checklist:**
- [ ] Page loads at `/memories/graph` without JS errors
- [ ] ~132 nodes visible by default
- [ ] Toggling books adds 16 diamonds
- [ ] Clicking NCNP HUB opens detail panel with its 29 neighbors
- [ ] Clicking a book diamond opens paginated chunk list
- [ ] Search "Magdy" dims everything except Dr. Magdy cluster
- [ ] Category filter "idea" dims non-idea nodes
- [ ] RTL layout works correctly (Arabic labels readable)
- [ ] Matches `base.html` cyberpunk theme (dark bg, green glow on hubs)

## Incremental Shipping Plan

| Slice | Scope | Verification | Rollback |
|-------|-------|--------------|----------|
| 1 | `core/graph_viz.py::get_backbone` + `/memories/graph.json` endpoint | `curl` returns valid JSON with stats | Delete new files |
| 2 | Empty page shell `pages/graph.html` + nav link in `base.html` | Visit `/memories/graph`, see empty container | Revert template files |
| 3 | Cytoscape init, fetch JSON, render default backbone | See ~132 dots with edges on canvas | Revert page.html |
| 4 | Filters: degree slider, category toggles, books toggle | All filters work without reload errors | Revert JS block |
| 5 | Detail panel endpoints + partials + click handlers | Click NCNP hub → see content; click book → see chunks | Revert partials + route |
| 6 | Polish: cyberpunk colors, hub glow, hover labels, legend | Visual match to `base.html` theme | Revert style block |

Each slice is an independent commit. The user can stop at any slice and still have a working page (or easily revert to the last good state).

**Total effort estimate:** ~2 hours coding spread across 6 deploys. Railway auto-deploy after each push.

## Out of Scope (v1)

The following are intentionally deferred to keep v1 focused:

- **Community detection** (Louvain coloring) — backbone force-layout already clusters naturally; coloring by Louvain community_id is a v2 enhancement
- **Vector-based 2D layout** (UMAP of embeddings) — would require embedding computation for entities; v2
- **Real-time updates** (SSE when a new link is created) — not needed for a read-only exploration view
- **Graph editing** (drag to create edges) — qmemory edits happen via MCP tools, not UI
- **Mobile layout** — desktop-first; split-pane collapses on narrow viewports but is not optimized for touch
- **Timeline mode** (nodes laid out by `created_at`) — interesting but separate feature

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| `WHERE in IN (...)` slow query in `get_backbone` | Traverse `SELECT in, out FROM relates` into Python, compute degree there. Documented in CLAUDE.md gotcha list |
| Cytoscape 3.29 Arabic font rendering | Use `Cairo` family explicitly in node label style (same as `base.html`) |
| `cose` layout slow with 166+ nodes | Measured: ~200ms on similar-scale graphs. Fine. Fallback to `concentric` if it becomes a problem |
| JSON payload size for 166 nodes | ~40KB estimated (200 bytes/node + 100 bytes/edge). Fine for direct fetch, no streaming needed |
| HTMX + Cytoscape interaction conflict | Use IIFE init (not `DOMContentLoaded`) to survive `hx-boost` page transitions. Pattern already proven in `nonprofit-knowledge-hub` |

## Reference Files Consulted

- `/Users/qusaiabushanap/dev/nonprofit-knowledge-hub/templates/pages/graph.html` — source pattern for Cytoscape + HTMX integration
- `/Users/qusaiabushanap/dev/student-companion/core/graph.py` — reference for async graph traversal functions in a SurrealDB codebase
- `qmemory/app/templates/base.html` — cyberpunk theme variables and base layout
- `qmemory/app/routes/memories.py` — pattern for route auth, template rendering, and session handling
- `qmemory/core/get.py::get_memories` with `include_neighbors=True` — existing function reused for ego drilldown
- `CLAUDE.md` — `WHERE in IN (...)` slow query gotcha, `MEMORY_FIELDS` constant, schema loader patterns
- Skills: `ultrathink-skills:hotwire-python-skill` (quad-interface pattern, `#NoBuild`), `ultrathink-skills:surrealdb-skill` (graph traversal `.{}` vs subquery, v3 BM25 gotchas)
