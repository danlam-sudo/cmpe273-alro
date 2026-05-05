import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from api_client import (chat, create_order, get_depots, get_orders,
                        get_plan_summary, run_plan, service_health,
                        update_order_priority, upload_inventory, upload_orders,
                        upload_vehicles)
from map_render import render_map

st.set_page_config(page_title="ALRO — Logistics Optimizer", layout="wide")
st.title("ALRO — Autonomous Logistics & Routing Optimizer")

# ── Service health bar ─────────────────────────────────────────────────────────
health = service_health()
cols = st.columns(len(health))
_labels = {"up": "✓ up", "down": "✗ down", "no_key": "⚠ no API key"}
_deltas = {"up": None, "down": None, "no_key": "chat disabled"}
for col, (name, state) in zip(cols, health.items()):
    col.metric(name, _labels.get(state, state), delta=_deltas.get(state))

# ── Tabs ───────────────────────────────────────────────────────────────────────
tab_data, tab_plan = st.tabs(["Data", "Route Plan"])

with tab_data:
    st.subheader("Upload Data")
    col1, col2, col3 = st.columns(3)

    with col1:
        st.caption("Orders CSV — columns: delivery_address (or delivery_lat/delivery_lon), units, priority")
        f = st.file_uploader("Orders CSV", type="csv", key="orders_csv")
        if f:
            df = pd.read_csv(f)
            result = upload_orders(df.to_dict("records"))
            st.success(f"{result['count']} orders loaded")

    with col2:
        st.caption("Vehicles CSV — columns: vehicle_id, depot_id, capacity_units")
        f = st.file_uploader("Vehicles CSV", type="csv", key="vehicles_csv")
        if f:
            df = pd.read_csv(f)
            upload_vehicles(df.to_dict("records"))
            st.success(f"{len(df)} vehicles loaded")

    with col3:
        st.caption("Inventory CSV — columns: warehouse_id, lat, lon, units_available")
        f = st.file_uploader("Inventory CSV", type="csv", key="inventory_csv")
        if f:
            df = pd.read_csv(f)
            upload_inventory(df.to_dict("records"))
            st.success(f"{len(df)} depots loaded")

    st.divider()

    # ── Manual order controls (mirrors the AI's tools) ─────────────────────────
    st.subheader("Manual Order Controls")
    ctrl_left, ctrl_right = st.columns(2)

    with ctrl_left:
        st.markdown("**Create Order**")
        with st.form("create_order_form", clear_on_submit=True):
            addr = st.text_input("Delivery address", placeholder="e.g. 200 E Santa Clara St, San Jose")
            units = st.number_input("Units", min_value=1, value=10, step=1)
            priority = st.selectbox("Priority", ["normal", "high"])
            if st.form_submit_button("Create Order", type="primary"):
                if not addr.strip():
                    st.error("Delivery address is required.")
                else:
                    try:
                        result = create_order(addr.strip(), int(units), priority)
                        oid = result["order_ids"][0]
                        st.success(f"Created — ID: `{oid}`")
                    except Exception as e:
                        st.error(str(e))

    with ctrl_right:
        st.markdown("**Update Order Priority**")
        with st.form("update_priority_form", clear_on_submit=True):
            order_id = st.text_input("Order ID", placeholder="Paste the full order UUID")
            new_priority = st.selectbox("New priority", ["normal", "high"], key="upd_priority")
            if st.form_submit_button("Update Priority", type="primary"):
                if not order_id.strip():
                    st.error("Order ID is required.")
                else:
                    try:
                        update_order_priority(order_id.strip(), new_priority)
                        st.success("Priority updated.")
                    except Exception as e:
                        st.error(str(e))

    st.divider()

    # ── Order list with search filters ────────────────────────────────────────
    st.subheader("Current Orders")
    f1, f2, _ = st.columns([1, 1, 3])
    with f1:
        filter_status = st.selectbox("Filter by status", ["all", "pending", "assigned", "delivered"])
    with f2:
        filter_priority = st.selectbox("Filter by priority", ["all", "normal", "high"])

    orders = get_orders(
        status=filter_status if filter_status != "all" else None,
        priority=filter_priority if filter_priority != "all" else None,
    )
    if orders:
        st.dataframe(pd.DataFrame(orders), use_container_width=True)
    else:
        st.info("No orders match the current filters.")

with tab_plan:
    plan_left, plan_right = st.columns([1, 2])

    with plan_left:
        if st.button("Run Optimization", type="primary", use_container_width=True):
            with st.spinner("Running LP allocation + MDVRP solver..."):
                try:
                    plan = run_plan()
                    st.session_state["current_plan"] = plan
                except Exception as e:
                    st.error(str(e))

        st.divider()

        # ── Get Plan Summary (mirrors the AI's get_plan_summary tool) ──────────
        st.markdown("**Get Plan Summary**")
        with st.form("plan_summary_form"):
            plan_id_input = st.text_input("Plan ID (leave blank for latest)", placeholder="latest")
            if st.form_submit_button("Fetch Summary"):
                pid = plan_id_input.strip() or "latest"
                try:
                    summary = get_plan_summary(pid)
                    kpis = summary.get("kpis", {})
                    st.metric("Orders Fulfilled", kpis.get("orders_fulfilled", "?"))
                    st.metric("Routes", len(summary.get("routes", [])))
                    st.metric("Avg Utilization", f"{kpis.get('avg_utilization_pct', '?')}%")
                    st.caption(f"Plan ID: `{summary.get('plan_id', pid)}`")
                except Exception as e:
                    st.error(str(e))

    with plan_right:
        if plan := st.session_state.get("current_plan"):
            kpis = plan.get("kpis", {})
            k1, k2, k3 = st.columns(3)
            k1.metric("Orders Fulfilled", kpis.get("orders_fulfilled", 0))
            k2.metric("Cross-Depot Splits", kpis.get("cross_depot_splits", 0))
            k3.metric("Avg Utilization", f"{kpis.get('avg_utilization_pct', 0)}%")

            if plan.get("partial"):
                st.warning("Plan is partial — solver reached time limit before finding the optimal solution.")
            if plan.get("routing_fallback"):
                st.info("Road distances unavailable — routes show straight-line approximations.")

            depots = get_depots()
            components.html(render_map(plan, depots), height=480)

            for i, route in enumerate(plan.get("routes", [])):
                with st.expander(
                    f"Truck {route['vehicle_id']} (depot {route['depot_id']}) — "
                    f"{route['utilization_pct']}% utilization, "
                    f"{route['total_distance_km']} km"
                ):
                    st.dataframe(pd.DataFrame(route["stops"]), use_container_width=True)
        else:
            st.info("No plan yet. Run optimization or fetch a plan by ID.")

# ── AI Chat sidebar ────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("AI Assistant")

    ai_state = health.get("ai")
    if ai_state == "down":
        st.warning("AI assistant offline — use the manual controls above.")
    elif ai_state == "no_key":
        st.warning("ANTHROPIC_API_KEY not set — AI chat is disabled. Use the manual controls above.")
    elif ai_state == "up":
        if "chat_history" not in st.session_state:
            st.session_state.chat_history = []
        if "session_id" not in st.session_state:
            st.session_state.session_id = None

        for msg in st.session_state.chat_history:
            with st.chat_message(msg["role"]):
                st.write(msg["content"])

        if prompt := st.chat_input("Ask or instruct..."):
            st.session_state.chat_history.append({"role": "user", "content": prompt})
            with st.spinner():
                try:
                    result = chat(prompt, st.session_state.session_id)
                    st.session_state.session_id = result["session_id"]
                    st.session_state.chat_history.append(
                        {"role": "assistant", "content": result["response"]}
                    )
                except Exception as e:
                    st.error(str(e))
            st.rerun()
