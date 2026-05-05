import httpx

PLANNER = "http://planner-service:8001"
AI      = "http://ai-service:8004"


def _raise_friendly(e: httpx.HTTPStatusError) -> None:
    """Re-raise with the detail message from the response body if available."""
    try:
        detail = e.response.json().get("detail", str(e))
    except Exception:
        detail = str(e)
    raise RuntimeError(detail) from None


def get_orders(**filters) -> list[dict]:
    clean = {k: v for k, v in filters.items() if v is not None and v != ""}
    r = httpx.get(f"{PLANNER}/data/orders", params=clean, timeout=5.0)
    r.raise_for_status()
    return r.json()


def upload_orders(orders: list[dict]) -> dict:
    r = httpx.post(f"{PLANNER}/data/orders", json={"orders": orders}, timeout=10.0)
    r.raise_for_status()
    return r.json()


def upload_vehicles(vehicles: list[dict]) -> dict:
    r = httpx.post(f"{PLANNER}/data/vehicles", json={"vehicles": vehicles}, timeout=10.0)
    r.raise_for_status()
    return r.json()


def upload_inventory(inventory: list[dict]) -> dict:
    r = httpx.post(f"{PLANNER}/data/inventory", json={"inventory": inventory}, timeout=10.0)
    r.raise_for_status()
    return r.json()


def create_order(delivery_address: str, units: int, priority: str) -> dict:
    try:
        r = httpx.post(
            f"{PLANNER}/data/orders",
            json={"orders": [{"delivery_address": delivery_address, "units": units, "priority": priority}]},
            timeout=15.0,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        _raise_friendly(e)


def update_order_priority(order_id: str, priority: str) -> dict:
    try:
        r = httpx.patch(
            f"{PLANNER}/data/orders/{order_id}",
            json={"priority": priority},
            timeout=5.0,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        _raise_friendly(e)


def get_plan_summary(plan_id: str = "latest") -> dict:
    try:
        r = httpx.get(f"{PLANNER}/plan/{plan_id}", timeout=5.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        _raise_friendly(e)


def get_depots() -> list[dict]:
    r = httpx.get(f"{PLANNER}/data/inventory", timeout=5.0)
    r.raise_for_status()
    return r.json()


def run_plan() -> dict:
    try:
        r = httpx.post(f"{PLANNER}/plan", timeout=20.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPStatusError as e:
        _raise_friendly(e)


def chat(message: str, session_id: str | None) -> dict:
    r = httpx.post(
        f"{AI}/chat",
        json={"message": message, "session_id": session_id},
        timeout=20.0,
    )
    r.raise_for_status()
    return r.json()


def service_health() -> dict[str, str]:
    """Returns a status string per service: 'up', 'down', or 'no_key' (ai only)."""
    simple = {
        "planner": f"{PLANNER}/health",
        "routing": "http://routing-service:8002/health",
        "solver":  "http://solver-service:8003/health",
    }
    result = {}
    for name, url in simple.items():
        try:
            result[name] = "up" if httpx.get(url, timeout=1.0).status_code == 200 else "down"
        except Exception:
            result[name] = "down"

    try:
        r = httpx.get(f"{AI}/health", timeout=1.0)
        if r.status_code == 200:
            result["ai"] = "up" if r.json().get("ai_configured") else "no_key"
        else:
            result["ai"] = "down"
    except Exception:
        result["ai"] = "down"

    return result
