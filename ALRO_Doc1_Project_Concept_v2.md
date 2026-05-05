# Autonomous Logistics & Routing Optimizer
## Project Concept & Architecture — v2

*Project Concept, Architecture & Component Guide*
Distributed Systems Course Project · ALRO v2.0

---

> **What this project is**
>
> ALRO is a logistics planning system that takes a batch of delivery orders, allocates
> inventory across multiple warehouses, and produces an optimized multi-truck itinerary
> on real Bay Area roads. It exposes two peer interfaces to the same planning engine:
> a standard UI with forms, tables, and direct actions; and an AI chat interface where
> a dispatcher describes intent in plain language and Claude fills in the same forms
> programmatically. Both call identical backend endpoints.
>
> **What it proves**
>
> That an AI tool-use layer and a standard UI can share the same deterministic API
> surface — and that the system degrades gracefully, with observable failure modes,
> when individual components go down. If the AI service fails, the standard UI
> continues working without modification.

---

## 1. Executive Summary

Logistics dispatchers at mid-size distributors face a daily planning problem that is
genuinely hard: given a set of delivery orders, a fleet of vehicles based at multiple
warehouses, and inventory that may be split across those warehouses, determine which
truck delivers which orders in what sequence. This planning task takes experienced
dispatchers 30–60 minutes of manual spreadsheet work each morning. When inventory
is short at one warehouse, orders are frequently missed or fulfilled incorrectly. When a
truck runs over capacity, re-planning happens verbally.

ALRO automates this workflow. A dispatcher uploads their orders, inventory levels, and
fleet configuration. The system validates that supply covers demand, runs a two-phase
optimization — first allocating inventory across depots, then routing vehicles — and
returns a complete itinerary in seconds.

The system exposes two peer interfaces to the same planning engine. The standard UI
provides forms for data entry, table views of orders and plans, and direct action
buttons. The AI chat interface accepts natural language — a dispatcher types what they
need, and Claude extracts the relevant parameters and calls the same underlying API
endpoints that the UI buttons would call. The two interfaces are not alternatives to each
other; they are complements. A dispatcher might upload a CSV through the UI and then
ask the chat to mark three specific orders as high priority before running the plan.

The project's engineering contribution is dual. On the business side, it demonstrates
that a standard deterministic planning API can be given a natural language interface
without modifying the planning system itself. On the systems side, it demonstrates that
a distributed architecture with proper observability — traces, metrics, circuit breakers,
and observable fallbacks — can be built and operated correctly at the scale of a course
project.

---

## 2. Design Philosophy & Scope

### 2.1 Core Thesis

A logistics planning API is more useful when two interfaces share it: a standard UI for
structured data operations, and an AI chat layer for the same operations expressed in
natural language. The planning engine is unchanged by which interface invokes it. The
AI layer adds accessibility without replacing the structured interface or coupling to it.

Beneath both interfaces, the system solves a real combinatorial problem: multi-depot
capacitated vehicle routing with inventory-constrained split delivery. This is the class
of problem that commercial logistics software charges hundreds of dollars per driver
per month to solve. The distributed architecture that hosts it is fault-tolerant, fully
observable, and designed so that individual service failures produce visible, recoverable
degradation rather than silent data loss.

### 2.2 Deliberate Simplifications

The following are intentional design boundaries, not limitations. Each removes
implementation risk without weakening the architectural argument.

| What we simplified | What we use instead | Why the argument still holds |
|---|---|---|
| Real-time traffic data | Static road distances (OSMnx graph) | Route optimization value is demonstrated without traffic; traffic is an additive data source |
| Full Bay Area road graph | San Jose only (OSMnx `graph_from_place("San Jose, California")`) | Multi-depot routing is fully demonstrable within one city; a larger graph increases download time, memory, and query time without adding architectural insight |
| SKU-level inventory tracking | Aggregate units per depot | The split-delivery and inventory allocation logic is identical regardless of product granularity |
| Time window constraints (VRPTW) | Capacity-only routing (CVRP) | Depot assignment and split delivery are the demonstrable hard decisions; time windows are additive |
| Cloud deployment | Local Docker Compose | Distributed systems patterns are fully demonstrable locally; cloud adds ops complexity without architectural insight |
| Managed metrics backend | Self-hosted Prometheus + Jaeger | All observability runs in Docker; no external accounts required |

### 2.3 What This Is NOT

- A production warehouse management or ERP system.
- A real-time traffic routing tool.
- A system that requires a cloud account or external API for core functionality.
- A machine learning training exercise — the optimization engine is a deterministic solver.
- A general-purpose AI assistant — the AI layer has exactly five callable operations and does nothing outside their scope.

---

## 3. System Architecture

### 3.1 Service Overview

The system consists of four backend microservices, a frontend dashboard, and an
observability stack. Each service boundary is a failure boundary — the decomposition
exists so that each component can fail, degrade, or become slow independently and
observably.

| Service | Port | Responsibility | Fails gracefully by |
|---|---|---|---|
| `planner-service` | 8001 | Orchestrator — coordinates downstream calls, owns all fallback logic, persists plan state | Returning partial plan with degraded flags set |
| `routing-service` | 8002 | OSMnx road graph, Haversine distance matrix, road-following geometry extraction | Planner falls back to straight-line Haversine distances |
| `solver-service` | 8003 | Inventory allocation LP (scipy) + OR-Tools MDVRP solver | Planner returns best solution found within time budget |
| `ai-service` | 8004 | Claude tool-use interface — interprets dispatcher messages, dispatches to planner endpoints | Planner completes and returns plan; chat interface shows service unavailable |
| `dashboard` | 8501 | Streamlit frontend — map, manifest table, KPI panel, AI chat sidebar | Per-service degradation indicators shown inline |

Observability infrastructure (local Docker containers, no external accounts):

| Container | Port | Purpose |
|---|---|---|
| `otel-collector` | 4317/4318 | Receives OpenTelemetry spans and metrics from all services |
| `jaeger` | 16686 | Distributed trace viewer |
| `prometheus` | 9090 | Metrics storage and query |

### 3.2 Request Flow — Happy Path

The following describes a full planning cycle. Steps 1 and 2 show the two interface
paths that both converge on the same `planner-service` endpoints.

1. **Standard UI path:** Dispatcher uploads three CSVs via the dashboard file upload
   forms (orders, vehicles, inventory), reviews the loaded tables, and clicks
   "Run Optimization." The dashboard calls `planner-service POST /plan` directly.

   **AI chat path:** Dispatcher types *"Run today's plan."* `ai-service` receives the
   message, identifies the intent as `trigger_replan`, and calls
   `planner-service POST /plan` — the same endpoint as the button above.

2. From this point the flow is identical regardless of which interface triggered it.

3. `planner-service` validates that total order demand does not exceed total inventory
   supply across all depots. If demand exceeds supply, it returns an error with a clear
   description of the shortfall before any planning begins.

4. `planner-service` calls `routing-service POST /distances` with the full list of depot
   and delivery locations. `routing-service` returns a distance matrix computed from
   the OSMnx Bay Area road graph.

5. `planner-service` calls `solver-service POST /solve` with the orders, distance
   matrix, and fleet configuration. `solver-service` runs in two phases:

   - **Phase 1 (Allocation LP):** A scipy linear program assigns inventory from depots
     to orders, minimizing total allocation cost. Orders that no single depot can cover
     alone are automatically split into sub-orders sourced from multiple depots.

   - **Phase 2 (MDVRP):** OR-Tools solves the multi-depot capacitated vehicle routing
     problem on the resulting sub-orders. Each vehicle has a fixed start and end depot.
     OR-Tools returns ordered stop sequences per vehicle.

6. `planner-service` calls `routing-service POST /geometry` with the stop sequences.
   `routing-service` returns the road-following polyline geometry for each route segment.

7. `planner-service` assembles the final Plan object — routes, ETAs, utilization
   percentages, KPIs — and returns it.

8. The dashboard renders: a Folium map with colored polylines per vehicle on real Bay
   Area roads, a per-vehicle manifest table, and a KPI panel.

9. All spans from steps 3–8 are collected by the OTel collector and visible as a single
   trace in Jaeger. All service metrics are scraped by Prometheus.

### 3.3 Fault Tolerance Paths

**`routing-service` unavailable or slow (> 2s timeout):**
`planner-service` activates the Haversine fallback. The distance matrix is computed
directly from lat/lon coordinates using the Haversine formula. Solving and geometry
steps proceed. The dashboard map shows straight-line routes between stops with a
visible indicator: *"Road geometry unavailable — showing approximate routes."*
The `fallback_activations_total{reason="routing_timeout"}` metric increments.

**`ai-service` unavailable:**
Because the standard UI calls `planner-service` directly, `ai-service` failure has no
effect on the standard interface. Forms, table actions, and the "Run Optimization"
button all continue working normally. The chat sidebar shows: *"AI assistant
temporarily unavailable — use the form interface."* No circuit breaker is needed on
the planner side; `ai-service` is a leaf node in the call graph, not a dependency of
the planning pipeline. The `claude_api_errors_total` metric on `ai-service` indicates
the failure, visible in Prometheus.

**`solver-service` hits time budget (5 seconds):**
OR-Tools is interrupted and returns the best solution found within the time limit. The
Plan is returned with `partial: true`. The dashboard displays a banner indicating the
plan may not be fully optimal. The dispatcher can trigger a replan with a larger time
budget via the chat.

---

## 4. Core Optimization — Two-Phase VRP

### 4.1 The Problem

Standard route optimization assumes a single depot and asks: in what sequence should
one or more vehicles visit a set of customers to minimize total distance, subject to
vehicle capacity? This is the Capacitated Vehicle Routing Problem (CVRP).

ALRO solves a harder variant. It has multiple depots, each with finite inventory. An
order may require more units than any single depot can supply, meaning it must be
fulfilled by combining stock from two depots — each sending a different vehicle to the
same delivery address. The system must decide both which depot sources which order
and how to route the resulting deliveries, simultaneously.

Solving these two decisions together in a single optimization is computationally
intractable at scale. ALRO uses a two-phase decomposition that separates them into
two well-understood sub-problems, solved sequentially.

### 4.2 Phase 1 — Inventory Allocation (Transportation LP)

Given depot supply levels and order demand quantities, the allocation phase finds the
minimum-cost assignment of inventory to orders. The cost metric is the Haversine
distance from each depot to each delivery address — a proxy for the transport cost of
sourcing from that depot.

This is a linear program (the Transportation Problem) with a closed-form network
structure. It runs in polynomial time and is solved by `scipy.optimize.linprog`. Any
order where no single depot has sufficient stock is automatically split: the LP assigns
a partial quantity from each of the depots that together cover the demand. The output
is a list of sub-orders, each fully allocated to one depot.

### 4.3 Phase 2 — Multi-Depot VRP (OR-Tools)

Sub-orders from Phase 1 are standard MDVRP nodes. Each sub-order has a fixed
sourcing depot, a delivery address, and a quantity. OR-Tools assigns sub-orders to
vehicles and sequences stops to minimize total route distance, subject to vehicle
capacity constraints.

The key configuration difference from single-depot CVRP: each vehicle has an
individual start and end depot node rather than a shared depot. OR-Tools handles the
customer-to-depot assignment as part of the optimization — it is not pre-assigned.

The gap between the two-phase solution and the true global optimum (which would
require solving allocation and routing simultaneously) is typically 2–5% on real
instances. This is the standard industry approach used by production logistics software.

---

## 5. Component Architecture

### 5.1 planner-service

The orchestrator. It is the only service the dashboard and `ai-service` call directly.
It owns the in-memory data store for orders, vehicles, inventory, and plans. All
fallback logic lives here — it is the single point responsible for deciding what happens
when a downstream service fails or is slow.

**Endpoints:**

| Endpoint | Method | Description |
|---|---|---|
| `/data/orders` | POST | Upload order list (CSV or JSON) |
| `/data/orders` | GET | List orders, supports filter params |
| `/data/orders/{id}` | PATCH | Update a single order (priority, status) |
| `/data/vehicles` | POST | Upload vehicle / fleet list |
| `/data/inventory` | POST | Upload inventory list |
| `/plan` | POST | Run full two-phase solve, return Plan |
| `/plan/{id}` | GET | Retrieve a completed plan |
| `/health` | GET | Liveness check |
| `/ready` | GET | Readiness check |
| `/metrics` | GET | Prometheus scrape endpoint |

**Fallback responsibilities:**
- 2-second timeout on `routing-service` with Haversine distance fallback
- 5-second time budget for `solver-service` with partial-result handling
- Structured JSON logging with `trace_id` and `span_id` on every log line

### 5.2 routing-service

Owns the San Jose OSMnx road graph. The graph is downloaded from OpenStreetMap
once at first startup, persisted to disk as a GraphML file, and loaded from disk on
subsequent starts. The `/ready` endpoint returns 503 until the graph is available,
preventing requests from arriving before the service can handle them.

The service has two responsibilities: computing the distance matrix between all
locations in a plan, and extracting road-following geometry for each route segment.

**Distance matrix strategy:**

A naïve all-pairs shortest path (one NetworkX query per location pair) is too slow for
a live demo — 33 locations produces 1,089 queries at 50–200ms each. Instead, the
service runs one single-source Dijkstra from each location across the full graph and
extracts distances to all other locations from that result. This reduces 1,089 queries
to 33 Dijkstra runs, completing in 2–5 seconds for San Jose scale. The matrix is
cached in memory keyed by the sorted set of location node IDs; only new locations
invalidate the cache.

**Startup sequence:**
1. Check for cached `sj_graph.graphml` on disk
2. If present, load it (`osmnx.load_graphml`) — takes ~3 seconds
3. If absent, download from OSM (`osmnx.graph_from_place("San Jose, California",
   network_type="drive")`), save to disk — takes 30–60 seconds on first run
4. Mark `/ready` as healthy

**Endpoints:**

| Endpoint | Method | Description |
|---|---|---|
| `/geocode` | POST | Accept an address string, return snapped lat/lon on the road graph via OSMnx + Nominatim. Used by `planner-service` for chat-originated orders. CSV uploads bypass this endpoint and provide lat/lon directly. |
| `/distances` | POST | Compute distance matrix (km) for a list of lat/lon locations using single-source Dijkstra |
| `/geometry` | POST | Extract road-following polyline for each stop-to-stop segment |
| `/health` | GET | Liveness check |
| `/ready` | GET | Returns 503 until graph is loaded and ready |
| `/metrics` | GET | Prometheus scrape endpoint |

**Key metrics:**
- `distance_matrix_duration_seconds` — histogram of matrix computation time
- `graph_cache_hits_total` / `graph_cache_misses_total` — cache effectiveness

### 5.3 solver-service

Runs both phases of the optimization. Phase 1 (allocation LP) always completes in
under 100ms. Phase 2 (OR-Tools VRP) scales with problem size and is subject to the
5-second time budget enforced by `planner-service`.

**Endpoints:**

| Endpoint | Method | Description |
|---|---|---|
| `/solve` | POST | Run Phase 1 + Phase 2, return Plan routes |
| `/health` | GET | Liveness check |
| `/metrics` | GET | Prometheus scrape endpoint |

**Key metrics:**
- `allocation_splits_total` — count of orders that required cross-depot sourcing
- `vrp_solve_duration_seconds` — histogram of solver time, labelled by order count

### 5.4 ai-service

A translation layer between natural language and `planner-service` API calls. Receives
dispatcher messages, selects the appropriate tool, extracts parameters from the
message, calls the matching `planner-service` endpoint, and returns a conversational
response. It has no direct knowledge of orders, vehicles, or plans — it only knows how
to call `planner-service` endpoints with well-formed arguments.

The tool definitions Claude receives are descriptions of the same `planner-service`
endpoints the standard UI calls. There is no separate tool layer — the tools are the
API. Claude fills in the parameters from natural language rather than the dispatcher
filling them in through a form.

The tool set is fixed and explicit. Claude has exactly five callable operations and
cannot perform any action outside of them. This is an intentional constraint — the AI
interface is scoped to planning operations, not a general assistant.

**Tools:**

| Tool | Example trigger | Parameters extracted |
|---|---|---|
| `create_order` | "Add an order, 80 units to the SAP Center, urgent" | delivery_address (string), units, priority |
| `update_order_priority` | "Mark order 14 as high priority" | order_id, priority |
| `search_orders` | "Show me all unassigned orders going to Palo Alto" | status, priority, area |
| `trigger_replan` | "Run the plan" / "Re-optimize with the new orders" | *(none)* |
| `get_plan_summary` | "Summarize today's routes" | plan_id |

`create_order` accepts a plain address string. `planner-service` calls `routing-service
POST /geocode` to resolve it to a snapped road-graph coordinate before persisting the
order. CSV uploads provide lat/lon directly and skip the geocode step entirely.

**Endpoints:**

| Endpoint | Method | Description |
|---|---|---|
| `/chat` | POST | Receive dispatcher message, dispatch tool, return response |
| `/health` | GET | Liveness check |
| `/metrics` | GET | Prometheus scrape endpoint |

**Key metric:** `claude_api_errors_total{reason}` — counter of Claude API failures by
reason. Because `ai-service` is a leaf node (no other service depends on it), its
failure is contained: only the chat interface is affected.

### 5.5 Dashboard

A Streamlit application with two peer interaction surfaces. The standard UI provides
direct form-based access to all planning operations. The AI chat sidebar provides
natural language access to the same operations via `ai-service`. A dispatcher may use
either or both — uploading a CSV through the UI and then asking the chat to modify
specific orders before triggering the plan.

**Standard UI panels:**

- **Data upload** — File upload forms for orders, vehicles, and inventory CSVs.
  Loaded data rendered as interactive tables with inline edit and filter controls.
  Direct action buttons: "Run Optimization", "Reset Orders", per-row priority toggle.

- **Map** — Folium map centered on the Bay Area. Depot markers, delivery pins colored
  by assigned vehicle, road-following polylines per vehicle.

- **Manifest** — Per-vehicle expandable section showing stop sequence, ETA at each
  stop, and utilization percentage.

- **KPI bar** — Plan generation time | Orders fulfilled | Cross-depot splits | Avg
  vehicle utilization.

- **Service health** — Inline status indicators for all four backend services. Degraded
  states (routing fallback active, partial plan, AI unavailable) shown with specific
  reason.

**AI chat sidebar** — Persistent across all panels. Calls `ai-service POST /chat`.
When `ai-service` is unavailable, the sidebar shows a status message and the standard
UI panels remain fully functional.

---

## 6. The AI Interface Layer — Design Rationale

### 6.1 AI as a peer interface, not a replacement

The standard UI and the AI chat are peers. The standard UI is better for structured,
repeatable operations: uploading a CSV, reviewing a plan table, toggling priority on a
specific row. The AI chat is better for operations that are easier to express in language
than to navigate through a form: *"add an urgent order, 300 units to Palo Alto, before
noon"* specifies four parameters in one sentence that would take four separate form
fields to enter manually.

The key architectural decision is that both interfaces call the same `planner-service`
endpoints. The AI chat does not have a separate backend. Claude extracts parameters
from natural language and calls `POST /data/orders` with the same request body that
the form submit button would produce. The planning system has no awareness of which
interface originated a request.

### 6.2 Why tool-use specifically

Claude's tool-use API enforces a hard boundary between language understanding and
system action. Claude interprets natural language and selects a tool. The tool
executes deterministically against the planning system. Claude does not write to the
data store, does not modify routes, and does not make optimization decisions.

This separation has two properties that matter in a logistics context. First, it is
auditable: every action taken through the AI interface corresponds to exactly one
deterministic API call with logged parameters. Second, it is bounded: Claude cannot
do anything the five tools do not permit. A dispatcher cannot accidentally trigger an
unintended operation by phrasing a message ambiguously — the tool schema constrains
what actions are possible.

### 6.3 What Claude does not do

Claude does not explain why OR-Tools made specific routing decisions. VRP solver
decisions are fully determined by the objective function and the inputs the dispatcher
provided — the distance matrix, the capacity constraints, the depot assignments.
There is no hidden reasoning to surface. The "why" of any routing decision is always
reducible to: this assignment minimized total distance subject to the constraints you
gave the system. That is visible in the data and legible on the map.

Adding a layer of AI-generated prose to restate what the map already shows would
add latency, cost tokens, and tell the dispatcher nothing they could not read for
themselves. Claude's role is interface, not narration.

---

## 7. Observability & Partial Failure

### 7.1 Instrumentation Approach

Every service is instrumented before business logic is written. Observability is not
added after the fact — it is a structural property of the system from the first line of
code. Each service exposes `/health`, `/ready`, and `/metrics` from its initial
scaffold, before any route optimization logic exists.

All services use the OpenTelemetry Python SDK. Spans are exported to the OTel
collector via gRPC and forwarded to Jaeger. Metrics are exported to Prometheus via
the standard scrape endpoint. Structured JSON logs include `trace_id` and `span_id`
on every line, enabling correlation between a specific log entry and the trace it
belongs to.

### 7.2 Distributed Trace Structure

A single `POST /plan` request produces a trace tree visible in Jaeger:

```
# Planning pipeline (same regardless of which interface triggered it)
planner-service: POST /plan
  ├── validate_inputs
  ├── routing-service: POST /distances
  │     └── compute_matrix
  ├── solver-service: POST /solve
  │     ├── allocation_lp
  │     └── vrp_optimize
  └── routing-service: POST /geometry
        └── extract_paths

# AI interface (separate call graph — leaf node, not part of planning pipeline)
ai-service: POST /chat
  └── planner-service: POST /data/orders   (or whichever tool was selected)
```

This trace answers the debugging question "where did this request spend its time"
without reading logs or adding print statements.

### 7.3 Key Metrics Per Service

| Service | Metric | Type | What it reveals |
|---|---|---|---|
| `planner-service` | `plan_duration_seconds` | Histogram | End-to-end planning latency |
| `planner-service` | `fallback_activations_total{reason}` | Counter | Which downstream failures triggered fallback |
| `solver-service` | `vrp_solve_duration_seconds` | Histogram | Solver performance by problem size |
| `solver-service` | `allocation_splits_total` | Counter | Frequency of cross-depot order splitting |
| `routing-service` | `distance_matrix_duration_seconds` | Histogram | Graph query performance |
| `ai-service` | `claude_api_duration_seconds` | Histogram | External API latency |
| `ai-service` | `claude_api_errors_total{reason}` | Counter | Claude API failure modes — leaf node, failure is contained to chat only |

### 7.4 Partial Failure Patterns

**Circuit breaker — `planner-service` → `ai-service`**

After three consecutive failures or timeouts, the circuit opens for 30 seconds. No
further calls are attempted during this window. Plans are produced and returned
normally. The dashboard chat sidebar shows the service as unavailable. When the
window expires, the circuit enters HALF_OPEN: a single probe request is allowed.
If it succeeds, the circuit closes. The state transition is visible on the metrics endpoint
in real time.

**Timeout + fallback — `planner-service` → `routing-service`**

A 2-second deadline is placed on all calls to `routing-service`. On timeout or
connection failure, `planner-service` computes the distance matrix directly using
the Haversine formula and proceeds. The plan is returned with a
`routing_degraded: true` flag. The dashboard map renders straight-line route
approximations with a visible indicator. The `fallback_activations_total` counter
increments with `reason="routing_timeout"`.

**Time-bounded solve — `solver-service`**

OR-Tools is configured with a 5-second wall-clock time limit via
`SolveWithParameters`. If the limit is reached before the optimal solution is found,
OR-Tools returns the best feasible solution found so far. The Plan is returned with
`partial: true`. The dashboard displays this status. The dispatcher can trigger a
replan via chat with an extended time budget.

---

## 8. Non-Functional Requirements

### 8.1 Performance

- `planner-service POST /plan` for 30 orders and 8 vehicles: < 8 seconds P95 end-to-end
  (dominated by OR-Tools solver time on the first run)
- `routing-service POST /distances` for 30 locations: < 2 seconds (time budget
  enforced by planner)
- `ai-service POST /chat`: < 4 seconds P95 (dominated by Claude API latency)
- Dashboard chat response: displayed as streaming text as Claude tokens arrive

### 8.2 Reliability

- No order loss: if any downstream service is unavailable, `planner-service` returns a
  degraded-mode response with `partial: true` rather than a 5xx error.
- In-memory state is the source of truth for the demo. No database is required.
  A service restart loses in-memory plan state — acceptable for a local demo context.
- The circuit breaker prevents cascading failure from a slow or unavailable `ai-service`
  from blocking plan generation.

### 8.3 Observability

- Every inter-service call produces a trace span with duration and outcome.
- Every planning cycle produces a complete trace from `/plan` entry to response return.
- All structured logs include `trace_id` for correlation with Jaeger.
- Circuit breaker state, fallback activations, and solver performance are all visible
  as Prometheus metrics without reading application code.

### 8.4 Deployment

- Single `docker-compose up` brings up all four services, the dashboard, OTel
  collector, Jaeger, and Prometheus with correct networking and port bindings.
- No cloud account required. Runs on any machine with Docker Desktop installed.
- Required environment variables: `ANTHROPIC_API_KEY`. All other configuration has
  sensible defaults.

---

## 9. Implementation Order

Instrumentation is added before business logic in each service. Every component is
observable from the moment it exists.

| Step | What gets built |
|---|---|
| 1 | Service scaffolding — all four FastAPI services with `/health`, `/ready`, `/metrics`, OTel wired up, Docker Compose network |
| 2 | `routing-service` — OSMnx Bay Area graph download, distance matrix, geometry extraction |
| 3 | `solver-service` Phase 1 — scipy LP inventory allocation, sub-order generation |
| 4 | `solver-service` Phase 2 — OR-Tools MDVRP model, multi-depot configuration |
| 5 | `planner-service` — orchestration logic, fallback handling, circuit breaker, data store |
| 6 | `ai-service` — Claude tool-use, five tool schemas, chat dispatch |
| 7 | `dashboard` — Folium map, manifest table, KPI panel, AI chat sidebar, degradation indicators |
| 8 | OTel collector + Jaeger + Prometheus configuration and Docker Compose wiring |

---

## 10. Glossary

| Term | Definition |
|---|---|
| CVRP | Capacitated Vehicle Routing Problem. Assign customers to vehicles and sequence stops to minimize total distance, subject to vehicle capacity limits. |
| MDVRP | Multi-Depot VRP. Extension of CVRP where vehicles depart from and return to different depot locations. |
| Transportation Problem | A linear programming problem that finds the minimum-cost assignment of supply from multiple sources to multiple destinations, subject to supply and demand constraints. Solved in polynomial time. |
| Sub-order | A runtime entity created when a single order requires inventory from more than one depot. Each sub-order is fully sourced from one depot and treated as an independent MDVRP node. |
| Split delivery | Fulfilling a single order from multiple depots because no single depot has sufficient stock. Produces two vehicles delivering to the same address. |
| Circuit breaker | A fault tolerance pattern that stops calling a failing downstream service after a threshold of failures, allows time for recovery, and resumes with a probe request. Prevents cascading failure. |
| Haversine fallback | Straight-line geographic distance used as a substitute for road-network distances when `routing-service` is unavailable. Less accurate but sufficient to produce a valid plan. |
| OpenTelemetry (OTel) | A vendor-neutral observability framework for generating and collecting distributed traces and metrics. The industry standard. |
| Partial plan | A Plan returned when OR-Tools reaches its time budget before finding the optimal solution. Contains the best feasible solution found, marked with `partial: true`. |
| Tool-use | The Claude API pattern where the model selects from a set of explicitly defined callable operations, extracts parameters from natural language, and returns a structured function call rather than free text. |
| Geocoding | Converting a human-readable address or place name into geographic coordinates (lat/lon). In ALRO, `routing-service` handles this via OSMnx's Nominatim wrapper, then snaps the result to the nearest node on the road graph. |
| Graph snapping | Finding the nearest node on the OSMnx road graph to a given lat/lon point. Required because geocoded coordinates or manually entered points may fall inside buildings or off-road; snapping ensures every location is reachable by the routing algorithm. |

---

*End of Document — ALRO v2 Project Concept & Architecture*
