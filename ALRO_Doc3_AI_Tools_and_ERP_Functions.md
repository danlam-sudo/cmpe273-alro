# AI Tools & ERP Function Reference
## ALRO — Autonomous Logistics & Routing Optimizer

*How the LLM's tools map to the underlying service functions*

---

## Overview

The AI agent (Claude) communicates with the user through natural language. When it
needs to take an action — create an order, search, trigger planning — it emits a
**tool call** from the set of five tools defined in `ai-service/tools.py`. The
`execute_tool()` dispatcher translates each tool call into an HTTP request to
`planner-service`. The planner endpoint validates the request, delegates to the
appropriate **store or orchestrator function**, and returns a result that is passed
back to Claude as the tool result for the next turn.

```
User message
    ↓
Claude (claude-sonnet-4-6) selects a tool
    ↓
execute_tool()          ai-service/tools.py
    ↓  HTTP
Planner endpoint        planner-service/main.py
    ↓
Store / Orchestrator    planner-service/store.py  or  orchestrator.py
```

Claude has access to **exactly five tools**. Each section below shows:

1. The tool schema (what Claude sees when deciding whether to use it)
2. The `execute_tool()` dispatch code
3. The planner-service HTTP endpoint
4. The underlying ERP function(s) — the layer that is completely independent of the AI

---

## Tool 1 — `create_order`

### 1.1 Tool schema

```python
{
    "name": "create_order",
    "description": (
        "Create a new delivery order. Use this when the dispatcher asks to add, "
        "create, or schedule a delivery."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "delivery_address": {
                "type": "string",
                "description": (
                    "Delivery address or place name in San Jose, CA. "
                    "Pass exactly what the dispatcher said — do not invent coordinates."
                ),
            },
            "units": {
                "type": "integer",
                "description": "Number of units to deliver.",
            },
            "priority": {
                "type": "string",
                "enum": ["normal", "high"],
                "description": "Order priority. Default to 'normal' if not specified.",
            },
        },
        "required": ["delivery_address", "units"],
    },
}
```

### 1.2 Tool dispatch — `execute_tool()` (`ai-service/tools.py`)

```python
if name == "create_order":
    r = await http.post(
        f"{PLANNER_URL}/data/orders",
        json={"orders": [inputs]},
        timeout=15.0,
    )
    r.raise_for_status()
    return f"Order created: {r.json()}"
```

Claude passes `delivery_address` (and optionally `priority`). The tool wraps it in
the `OrdersUpload` envelope `{"orders": [...]}` expected by the planner endpoint.

### 1.3 Planner endpoint — `POST /data/orders` (`planner-service/main.py`)

```python
@app.post("/data/orders")
async def create_orders(req: OrdersUpload):
    resolved = []
    for o in req.orders:
        d = o.dict()
        if d.get("delivery_address") and not (d.get("delivery_lat") and d.get("delivery_lon")):
            lat, lon = await geocode(d["delivery_address"])   # ← routing-service call
            d["delivery_lat"] = lat
            d["delivery_lon"] = lon
        resolved.append(d)
    ids = store.add_orders(resolved)
    return {"order_ids": ids, "count": len(ids)}
```

When Claude passes only a `delivery_address`, the endpoint calls
`orchestrator.geocode()` to resolve it to coordinates before storing.

**Geocoding path** (`planner-service/orchestrator.py` → `routing-service`):

```python
async def geocode(address: str) -> tuple[float, float]:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{ROUTING_URL}/geocode",          # routing-service:8002
            json={"address": address},
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()
        return data["lat"], data["lon"]
```

### 1.4 Underlying ERP function — `Store.add_orders()` (`planner-service/store.py`)

```python
def add_orders(self, orders: list[dict]) -> list[str]:
    ids = []
    for o in orders:
        oid = o.get("order_id") or str(uuid.uuid4())
        fields = {k: v for k, v in o.items() if k != "order_id"}
        self._orders[oid] = OrderRecord(order_id=oid, **fields)
        ids.append(oid)
    return ids
```

This function is the authoritative write path for orders regardless of whether the
caller is the AI, a CSV upload from the dashboard, or a direct API call. It assigns
a UUID if none is provided and stores an `OrderRecord` dataclass:

```python
@dataclass
class OrderRecord:
    order_id: str
    units: int
    priority: str
    delivery_address: Optional[str] = None
    delivery_lat: Optional[float] = None
    delivery_lon: Optional[float] = None
    status: str = "pending"
    created_at: str = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
```

---

## Tool 2 — `update_order_priority`

### 2.1 Tool schema

```python
{
    "name": "update_order_priority",
    "description": "Change the priority of an existing order.",
    "input_schema": {
        "type": "object",
        "properties": {
            "order_id": {"type": "string"},
            "priority": {"type": "string", "enum": ["normal", "high"]},
        },
        "required": ["order_id", "priority"],
    },
}
```

Claude must know the `order_id` before calling this tool. It typically calls
`search_orders` first to find the ID, then calls `update_order_priority`.

### 2.2 Tool dispatch — `execute_tool()` (`ai-service/tools.py`)

```python
elif name == "update_order_priority":
    r = await http.patch(
        f"{PLANNER_URL}/data/orders/{inputs['order_id']}",
        json={"priority": inputs["priority"]},
        timeout=5.0,
    )
    r.raise_for_status()
    return "Priority updated."
```

### 2.3 Planner endpoint — `PATCH /data/orders/{order_id}` (`planner-service/main.py`)

```python
@app.patch("/data/orders/{order_id}")
def update_order(order_id: str, updates: OrderUpdate):
    try:
        store.patch_order(order_id, {k: v for k, v in updates.dict().items() if v is not None})
        return {"status": "updated"}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
```

`OrderUpdate` accepts both `priority` and `status` fields, so the same endpoint
is used by the dashboard's manual priority form and by status tracking.

### 2.4 Underlying ERP function — `Store.patch_order()` (`planner-service/store.py`)

```python
def patch_order(self, order_id: str, updates: dict):
    if order_id not in self._orders:
        raise KeyError(order_id)
    for k, v in updates.items():
        setattr(self._orders[order_id], k, v)
```

A generic field-level patch on the `OrderRecord`. Raises `KeyError` if the order
does not exist; the endpoint converts this to a 404.

---

## Tool 3 — `search_orders`

### 3.1 Tool schema

```python
{
    "name": "search_orders",
    "description": (
        "Search orders. Use this to look up orders by address, status, or priority — "
        "including when you need to find an order_id before calling update_order_priority. "
        "Returns up to 10 matches."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "delivery_address": {
                "type": "string",
                "description": "Partial address string to search for (case-insensitive substring match).",
            },
            "status":   {"type": "string", "enum": ["pending", "assigned", "delivered"]},
            "priority": {"type": "string", "enum": ["normal", "high"]},
        },
    },
}
```

All three fields are optional. Claude can call this with no arguments to list all
orders, or combine filters.

### 3.2 Tool dispatch — `execute_tool()` (`ai-service/tools.py`)

```python
elif name == "search_orders":
    params = {k: v for k, v in inputs.items() if v}
    # delivery_address maps to the 'address' query param on planner-service
    if "delivery_address" in params:
        params["address"] = params.pop("delivery_address")
    r = await http.get(
        f"{PLANNER_URL}/data/orders",
        params=params,
        timeout=5.0,
    )
    r.raise_for_status()
    orders = r.json()
    if not orders:
        return "No orders matched."
    return f"{len(orders)} order(s) found: {orders[:10]}"
```

Note the key rename: the tool uses `delivery_address` (matching Claude's vocabulary),
but the planner query parameter is `address`. The dispatch layer bridges the naming gap.

### 3.3 Planner endpoint — `GET /data/orders` (`planner-service/main.py`)

```python
@app.get("/data/orders")
def list_orders(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    address: Optional[str] = None,
):
    return store.get_orders(status=status, priority=priority, address=address)
```

### 3.4 Underlying ERP function — `Store.get_orders()` (`planner-service/store.py`)

```python
def get_orders(
    self,
    status: Optional[str] = None,
    priority: Optional[str] = None,
    address: Optional[str] = None,
) -> list[dict]:
    records = list(self._orders.values())
    if status:
        records = [r for r in records if r.status == status]
    if priority:
        records = [r for r in records if r.priority == priority]
    if address:
        records = [
            r for r in records
            if r.delivery_address and address.lower() in r.delivery_address.lower()
        ]
    return [vars(r) for r in records]
```

All three filters are exact-match except `address`, which is a case-insensitive
substring search against `delivery_address`. Filters compose with AND semantics.

---

## Tool 4 — `trigger_replan`

### 4.1 Tool schema

```python
{
    "name": "trigger_replan",
    "description": "Run route optimization with the current set of orders, vehicles, and inventory.",
    "input_schema": {"type": "object", "properties": {}},
}
```

No inputs. Claude calls this when the dispatcher asks to re-optimize, or after
creating/updating orders that require route changes.

### 4.2 Tool dispatch — `execute_tool()` (`ai-service/tools.py`)

```python
elif name == "trigger_replan":
    r = await http.post(f"{PLANNER_URL}/plan", timeout=20.0)
    r.raise_for_status()
    data = r.json()
    kpis = data["kpis"]
    msg = (
        f"Plan complete. {kpis['orders_fulfilled']} orders fulfilled, "
        f"{kpis['cross_depot_splits']} cross-depot splits, "
        f"{kpis['avg_utilization_pct']}% avg utilization."
    )
    if data.get("partial"):
        msg += " (partial — solver hit time limit)"
    if data.get("routing_fallback"):
        msg += " (road distances unavailable — used straight-line estimate)"
    skipped = data.get("skipped_orders", [])
    deferred = [s for s in skipped if s.get("reason", "").startswith("deferred")]
    if deferred:
        msg += (
            f" {len(deferred)} order(s) deferred due to insufficient supply: "
            + ", ".join(s.get("address") or s.get("order_id", "?") for s in deferred)
        )
    elif kpis.get("orders_skipped", 0):
        msg += f" {kpis['orders_skipped']} order(s) skipped (no coordinates)."
    return msg
```

The dispatch formats a human-readable summary from the KPI fields so Claude can
relay the result to the dispatcher without exposing the raw JSON.

### 4.3 Planner endpoint — `POST /plan` (`planner-service/main.py`)

The `/plan` endpoint is the most complex in the system. It runs the full optimization
pipeline, handling partial failures gracefully at each stage.

```
POST /plan
    │
    ├─ store.get_orders() / get_depots() / get_vehicles()
    │
    ├─ oversized order filter   (orders > max vehicle capacity → skipped)
    │
    ├─ supply demotion           (if total demand > supply, defer low-priority orders)
    │
    ├─ orchestrator.get_distance_matrix(locations)
    │       ├─ routing-service POST /distances   (road Dijkstra matrix)
    │       └─ fallback: _haversine_matrix()     (straight-line distances)
    │
    ├─ orchestrator.run_solve(orders, depots, vehicles, matrix, locations)
    │       └─ solver-service POST /solve
    │               ├─ Phase 1: allocation.allocate()   (scipy LP)
    │               └─ Phase 2: vrp.solve()             (OR-Tools MDVRP)
    │
    ├─ orchestrator.get_route_geometries(segments)
    │       ├─ routing-service POST /geometry   (road-following polylines)
    │       └─ fallback: straight-line segments
    │
    └─ store.save_plan(result)   → returns plan_id
```

### 4.4 Underlying ERP functions

**`Store.get_orders()` / `get_depots()` / `get_vehicles()`** — read the current
in-memory state (see Tool 3 for `get_orders`).

**`Store.save_plan()`** — persists the completed plan and returns its ID:

```python
def save_plan(self, plan: dict) -> str:
    plan_id = str(uuid.uuid4())
    plan["plan_id"] = plan_id
    self._plans[plan_id] = plan
    return plan_id
```

**`orchestrator.get_distance_matrix()`** — fetches road distances, falls back to
Haversine if routing-service is unreachable:

```python
async def get_distance_matrix(
    locations: list[tuple[float, float]],
) -> tuple[list[list[float]], bool]:
    try:
        async with asyncio.timeout(2.0):
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{ROUTING_URL}/distances",
                    json={"locations": locations},
                    timeout=10.0,
                )
                r.raise_for_status()
                return r.json()["matrix"], False
    except (asyncio.TimeoutError, httpx.ConnectError, httpx.TimeoutException):
        return _haversine_matrix(locations), True
```

**`orchestrator.run_solve()`** — sends the optimization problem to solver-service:

```python
async def run_solve(
    orders, depots, vehicles, distance_matrix, locations,
) -> tuple[dict, bool]:
    async with httpx.AsyncClient() as client:
        r = await client.post(
            f"{SOLVER_URL}/solve",
            json={
                "orders": orders,
                "depots": depots,
                "vehicles": vehicles,
                "distance_matrix": distance_matrix,
                "locations": locations,
                "time_limit_seconds": 5,
            },
            timeout=12.0,
        )
        r.raise_for_status()
        data = r.json()
        return data, data.get("partial", False)
```

**`orchestrator.get_route_geometries()`** — fetches road-following polylines for
the map display, falls back to straight segments:

```python
async def get_route_geometries(
    segments: list[tuple],
) -> list[list[tuple]]:
    try:
        async with asyncio.timeout(5.0):
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    f"{ROUTING_URL}/geometry",
                    json={"segments": segments},
                    timeout=10.0,
                )
                r.raise_for_status()
                return r.json()["paths"]
    except Exception:
        return [[origin, destination] for origin, destination in segments]
```

---

## Tool 5 — `get_plan_summary`

### 5.1 Tool schema

```python
{
    "name": "get_plan_summary",
    "description": "Retrieve and summarize the most recent completed plan.",
    "input_schema": {
        "type": "object",
        "properties": {
            "plan_id": {
                "type": "string",
                "description": "Optional plan ID. Omit to get the latest plan.",
            },
        },
    },
}
```

### 5.2 Tool dispatch — `execute_tool()` (`ai-service/tools.py`)

```python
elif name == "get_plan_summary":
    plan_id = inputs.get("plan_id", "latest")
    r = await http.get(f"{PLANNER_URL}/plan/{plan_id}", timeout=5.0)
    r.raise_for_status()
    data = r.json()
    kpis = data.get("kpis", {})
    return (
        f"Plan {data.get('plan_id', plan_id)}: "
        f"{kpis.get('orders_fulfilled', '?')} orders, "
        f"{len(data.get('routes', []))} routes, "
        f"{kpis.get('avg_utilization_pct', '?')}% avg utilization."
    )
```

If the dispatcher omits `plan_id`, it defaults to `"latest"`, which the planner
endpoint resolves to the most recently saved plan.

### 5.3 Planner endpoint — `GET /plan/{plan_id}` (`planner-service/main.py`)

```python
@app.get("/plan/{plan_id}")
def get_plan(plan_id: str):
    plan = (
        store.get_latest_plan() if plan_id == "latest"
        else store.get_plan(plan_id)
    )
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    return plan
```

### 5.4 Underlying ERP functions — `Store.get_plan()` / `Store.get_latest_plan()` (`planner-service/store.py`)

```python
def get_plan(self, plan_id: str) -> Optional[dict]:
    return self._plans.get(plan_id)

def get_latest_plan(self) -> Optional[dict]:
    if not self._plans:
        return None
    return self._plans[next(reversed(self._plans))]   # insertion-ordered in Python 3.7+
```

`get_latest_plan()` relies on Python's dict insertion ordering to return the most
recently saved plan without maintaining a separate pointer.

---

## Full Call Chain Summary

| Tool | Claude input | `execute_tool()` call | Planner endpoint | ERP function(s) |
|---|---|---|---|---|
| `create_order` | `delivery_address`, `units`, `priority` | `POST /data/orders` | `create_orders()` | `store.add_orders()`, `orchestrator.geocode()` |
| `update_order_priority` | `order_id`, `priority` | `PATCH /data/orders/{id}` | `update_order()` | `store.patch_order()` |
| `search_orders` | `delivery_address?`, `status?`, `priority?` | `GET /data/orders` | `list_orders()` | `store.get_orders()` |
| `trigger_replan` | *(none)* | `POST /plan` | `plan()` | `store.get_orders/depots/vehicles()`, `orchestrator.get_distance_matrix()`, `orchestrator.run_solve()`, `orchestrator.get_route_geometries()`, `store.save_plan()` |
| `get_plan_summary` | `plan_id?` | `GET /plan/{plan_id}` | `get_plan()` | `store.get_plan()` or `store.get_latest_plan()` |

---

## Notes on the Agentic Loop

Claude does not see HTTP status codes or raw JSON. It only sees the string returned
by `execute_tool()`. The loop in `handle_message()` allows Claude to chain tool
calls — for example, calling `search_orders` to find an ID and then immediately
calling `update_order_priority` with that ID, all within a single user turn:

```python
MAX_STEPS = 5
for _ in range(MAX_STEPS):
    response = _get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=messages,
    )
    if response.stop_reason != "tool_use":
        break
    # execute all tool_use blocks, append results, continue loop
```

Multiple tool calls can appear in a single response (e.g., Claude creating two
orders simultaneously). Each block is executed and its result returned in the same
`tool_results` message.

---

*End of AI Tools & ERP Function Reference — ALRO*
