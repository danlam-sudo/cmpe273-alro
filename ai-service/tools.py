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

You have exactly five tools. Use them to take actions when the dispatcher makes a
request. If a request cannot be handled by one of your tools, say so clearly.

When creating orders, pass the address exactly as the dispatcher described it.
Do not generate or guess lat/lon coordinates — the system resolves addresses itself.

Be concise. One or two sentences is usually enough.
"""


async def execute_tool(name: str, inputs: dict) -> str:
    """Maps tool name to planner-service endpoint. Returns a string for tool_result."""
    async with httpx.AsyncClient() as http:
        try:
            if name == "create_order":
                r = await http.post(
                    f"{PLANNER_URL}/data/orders",
                    json={"orders": [inputs]},
                    timeout=15.0,
                )
                r.raise_for_status()
                return f"Order created: {r.json()}"

            elif name == "update_order_priority":
                r = await http.patch(
                    f"{PLANNER_URL}/data/orders/{inputs['order_id']}",
                    json={"priority": inputs["priority"]},
                    timeout=5.0,
                )
                r.raise_for_status()
                return "Priority updated."

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

        except httpx.HTTPStatusError as e:
            return f"Error {e.response.status_code}: {e.response.text}"
        except Exception as e:
            return f"Tool execution failed: {e}"

    return "Unknown tool."


async def handle_message(session_id: str, message: str) -> str:
    from session import append, get_history

    history = get_history(session_id)
    append(session_id, "user", message)

    messages = history + [{"role": "user", "content": message}]

    # Agentic loop — Claude calls tools as needed, chaining them until it has
    # enough information to give a final answer. Capped at 5 iterations to
    # prevent runaway loops.
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

        # Execute every tool block in this response (usually one, occasionally more)
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            log.info("chat_tool_call", session=session_id, tool=block.name, inputs=block.input)
            result = await execute_tool(block.name, block.input)
            log.info("chat_tool_result", session=session_id, tool=block.name, result=result)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    reply = next(
        (block.text for block in response.content if hasattr(block, "text")),
        "I was unable to complete that request.",
    )

    log.info("chat_turn", session=session_id, user=message, reply=reply)
    append(session_id, "assistant", reply)
    return reply
