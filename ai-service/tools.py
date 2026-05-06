import anthropic
import httpx
import structlog

log = structlog.get_logger()

PLANNER_URL = "http://planner-service:8001"

# Lazily initialized so ai-service starts cleanly even without ANTHROPIC_API_KEY.
# The key is only required when /chat is actually called.
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env
    return _client

TOOLS = [
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
    },
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
    },
    {
        "name": "search_orders",
        "description": (
            "Search and filter orders. Use this to look up orders by address, status, priority, "
            "unit count, warehouse assignment, or creation date — including when you need to find "
            "an order_id before calling update_order_priority. Returns up to 10 matches."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "delivery_address": {
                    "type": "string",
                    "description": "Partial address string to search for (case-insensitive substring match).",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "assigned", "delivered"],
                },
                "priority": {
                    "type": "string",
                    "enum": ["normal", "high"],
                },
                "units_min": {
                    "type": "integer",
                    "description": "Return only orders with this many units or more.",
                },
                "units_max": {
                    "type": "integer",
                    "description": "Return only orders with this many units or fewer.",
                },
                "depot_id": {
                    "type": "string",
                    "description": (
                        "Return only orders assigned to this warehouse ID in the most recent plan "
                        "(e.g. 'W1' or 'W2'). Requires a plan to have been run."
                    ),
                },
                "created_after": {
                    "type": "string",
                    "description": (
                        "ISO 8601 date-time string. Return only orders created at or after this time "
                        "(e.g. '2026-05-01T00:00:00Z')."
                    ),
                },
            },
        },
    },
    {
        "name": "apply_filter",
        "description": (
            "Pre-populate the Data tab's order filter so the dispatcher can see the full matching "
            "list in the UI. Use this whenever the dispatcher asks to 'show', 'list', 'display', "
            "or 'see' orders — do NOT use search_orders for display requests. This tool fetches "
            "nothing and reasons about nothing; it just wires up the filter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "priority": {
                    "type": "string",
                    "enum": ["normal", "high"],
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "assigned", "delivered"],
                },
                "delivery_address": {
                    "type": "string",
                    "description": "Partial address substring to pre-fill in the address filter.",
                },
                "units_min": {
                    "type": "integer",
                    "description": "Show only orders with this many units or more.",
                },
                "units_max": {
                    "type": "integer",
                    "description": "Show only orders with this many units or fewer.",
                },
                "depot_id": {
                    "type": "string",
                    "description": "Show only orders assigned to this warehouse (requires a plan to exist).",
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["units", "priority", "created_at", "status"],
                },
                "sort_order": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                },
            },
        },
    },
    {
        "name": "trigger_replan",
        "description": "Run route optimization with the current set of orders, vehicles, and inventory.",
        "input_schema": {"type": "object", "properties": {}},
    },
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
    },
]

SYSTEM_PROMPT = """
You are a logistics dispatch assistant for a San Jose delivery operation.
You help dispatchers manage orders, check status, and trigger route planning.

You have six tools. Choose between them based on what the dispatcher actually wants:

DISPLAY vs REASONING — this is the most important distinction:
- Use apply_filter when the dispatcher wants to SEE a list: "show me", "list all",
  "display", "what are all the...". This pre-populates the UI filter without loading
  data into this conversation. Never use search_orders for display requests.
- Use search_orders when the dispatcher needs you to REASON about data: "how many",
  "which order has the most", "find the order ID for", "compare". This loads up to
  25 orders into context so you can answer analytically.

When creating orders, pass the address exactly as the dispatcher described it.
Do not generate or guess lat/lon coordinates — the system resolves addresses itself.

One user request = one write action. Do not call create_order, update_order_priority,
or trigger_replan more than once per turn regardless of how many times the dispatcher
phrases the request.

Never mention internal system behavior, tool names, or constraints to the dispatcher.
Just confirm what was done in plain language.

Be concise. One or two sentences is usually enough.
"""


async def execute_tool(name: str, inputs: dict) -> tuple[str, dict]:
    """
    Maps tool name to planner-service endpoint.
    Returns (result_string_for_claude, side_effects).
    side_effects is a plain dict with optional keys:
      "pending_filter" — dashboard pre-populates filter widgets
      "pending_plan"   — dashboard loads plan into Route Plan tab
    """
    if name == "apply_filter":
        params = {k: v for k, v in inputs.items() if v is not None and v != ""}
        if not params:
            return "Filters cleared — the Data tab will now show all orders.", {"pending_filter": {}}
        label_parts = []
        if "priority" in params:
            label_parts.append(f"{params['priority']}-priority")
        if "status" in params:
            label_parts.append(f"status={params['status']}")
        if "delivery_address" in params:
            label_parts.append(f"address contains '{params['delivery_address']}'")
        if "units_min" in params:
            label_parts.append(f"≥{params['units_min']} units")
        if "units_max" in params:
            label_parts.append(f"≤{params['units_max']} units")
        if "depot_id" in params:
            label_parts.append(f"depot={params['depot_id']}")
        label = ", ".join(label_parts) or "custom"
        return f"Filter applied ({label}). The Data tab will show all matching orders.", {"pending_filter": params}

    async with httpx.AsyncClient() as http:
        try:
            if name == "create_order":
                r = await http.post(
                    f"{PLANNER_URL}/data/orders",
                    json={"orders": [inputs]},
                    timeout=15.0,
                )
                r.raise_for_status()
                return f"Order created: {r.json()}", {}

            elif name == "update_order_priority":
                r = await http.patch(
                    f"{PLANNER_URL}/data/orders/{inputs['order_id']}",
                    json={"priority": inputs["priority"]},
                    timeout=5.0,
                )
                r.raise_for_status()
                return "Priority updated.", {}

            elif name == "search_orders":
                params = {k: v for k, v in inputs.items() if v is not None and v != ""}
                if "delivery_address" in params:
                    params["address"] = params.pop("delivery_address")
                params.setdefault("limit", 25)
                r = await http.get(
                    f"{PLANNER_URL}/data/orders",
                    params=params,
                    timeout=5.0,
                )
                r.raise_for_status()
                orders = r.json()
                if not orders:
                    return "No orders matched the given filters.", {}
                return f"{len(orders)} order(s) found: {orders}", {}

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
                return msg, {"pending_plan": data}

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
                ), {}

        except httpx.HTTPStatusError as e:
            return f"Error {e.response.status_code}: {e.response.text}", {}
        except Exception as e:
            return f"Tool execution failed: {e}", {}

    return "Unknown tool.", {}


async def handle_message(session_id: str, message: str) -> tuple[str, dict]:
    """
    Returns (reply, side_effects).
    side_effects may contain:
      "pending_filter" — dashboard pre-populates filter widgets
      "pending_plan"   — dashboard loads plan into Route Plan tab and navigates there
    """
    from session import append, get_history

    MAX_HISTORY = 40  # ~20 conversation turns; keeps context manageable
    history = get_history(session_id)
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
        while history and history[0]["role"] != "user":
            history = history[1:]
    append(session_id, "user", message)

    messages = history + [{"role": "user", "content": message}]
    side_effects: dict = {}

    MAX_STEPS = 5
    for _ in range(MAX_STEPS):
        response = _get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            break

        _MUTATING_TOOLS = {"create_order", "update_order_priority", "trigger_replan"}
        _seen_calls: set[tuple] = set()
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            # Deduplicate identical mutating tool calls within one response turn.
            if block.name in _MUTATING_TOOLS:
                call_key = (block.name, tuple(sorted(block.input.items())))
                if call_key in _seen_calls:
                    log.warning("chat_tool_duplicate_skipped", session=session_id, tool=block.name, inputs=block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Duplicate call skipped — this tool was already called with the same inputs this turn.",
                    })
                    continue
                _seen_calls.add(call_key)
            log.info("chat_tool_call", session=session_id, tool=block.name, inputs=block.input)
            result_str, fx = await execute_tool(block.name, block.input)
            side_effects.update(fx)
            log.info("chat_tool_result", session=session_id, tool=block.name, result=result_str)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    reply = next(
        (block.text for block in response.content if hasattr(block, "text")),
        "I was unable to complete that request.",
    )

    log.info("chat_turn", session=session_id, user=message, reply=reply)
    append(session_id, "assistant", reply)
    return reply, side_effects
