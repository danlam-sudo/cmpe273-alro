import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from api_client import (chat, create_order, get_depots, get_orders,
                        get_plan_summary, get_vehicles, run_plan, service_health,
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

if st.session_state.pop("_switch_to_plan_tab", False):
    components.html("""
        <script>
        setTimeout(function() {
            const tabs = window.parent.document.querySelectorAll('[data-baseweb="tab"]');
            for (const tab of tabs) {
                if (tab.innerText.trim() === 'Route Plan') { tab.click(); break; }
            }
        }, 150);
        </script>
    """, height=0)

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

    # Apply any filter the AI assistant pre-populated via apply_filter.
    # The chat sidebar stores it in _pending_filter; we flush it here on each
    # render so the widgets pick up the new values via their session state keys.
    if "_pending_filter" in st.session_state:
        pf = st.session_state.pop("_pending_filter")
        if not pf:
            # Empty dict = "show all" — reset every widget to its default
            st.session_state["filter_status"] = "all"
            st.session_state["filter_priority"] = "all"
            st.session_state["filter_address"] = ""
            st.session_state["filter_depot"] = "(all)"
            st.session_state["filter_units_min"] = 0
            st.session_state["filter_units_max"] = 0
            st.session_state["filter_sort"] = "none"
            st.session_state["filter_sort_order"] = "asc"
            st.info("Filters cleared — showing all orders.")
        else:
            if "priority" in pf:
                st.session_state["filter_priority"] = pf["priority"]
            if "status" in pf:
                st.session_state["filter_status"] = pf["status"]
            if "delivery_address" in pf:
                st.session_state["filter_address"] = pf["delivery_address"]
            if "units_min" in pf:
                st.session_state["filter_units_min"] = int(pf["units_min"])
            if "units_max" in pf:
                st.session_state["filter_units_max"] = int(pf["units_max"])
            if "depot_id" in pf:
                st.session_state["filter_depot"] = pf["depot_id"]
            if "sort_by" in pf:
                st.session_state["filter_sort"] = pf["sort_by"]
            if "sort_order" in pf:
                st.session_state["filter_sort_order"] = pf["sort_order"]
            st.info("Filters set by AI assistant — showing all matching orders below.")

    # Fetch depot list once per render to power the depot dropdown.
    try:
        _depots = get_depots()
        _depot_options = ["(all)"] + [d["warehouse_id"] for d in _depots]
    except Exception:
        _depots = []
        _depot_options = ["(all)"]

    fa, fb, fc, fd, fe = st.columns([1, 1, 1, 1, 1])
    with fa:
        filter_status = st.selectbox(
            "Status", ["all", "pending", "assigned", "delivered"], key="filter_status"
        )
    with fb:
        filter_priority = st.selectbox(
            "Priority", ["all", "normal", "high"], key="filter_priority"
        )
    with fc:
        filter_address = st.text_input(
            "Address contains", placeholder="e.g. Santana Row", key="filter_address"
        )
        # Ensure any AI-injected depot value is a valid option; fall back to "(all)" if not.
        if st.session_state.get("filter_depot") not in _depot_options:
            st.session_state["filter_depot"] = "(all)"
        filter_depot = st.selectbox(
            "Depot (post-plan)",
            _depot_options,
            key="filter_depot",
            help="Shows orders assigned to this depot in the latest plan. "
                 "Run optimization first.",
        )
    with fd:
        filter_units_min = st.number_input(
            "Min units", min_value=0, value=0, step=1, key="filter_units_min"
        )
        filter_units_max = st.number_input(
            "Max units", min_value=0, value=0, step=1,
            help="0 = no upper limit", key="filter_units_max"
        )
    with fe:
        filter_sort = st.selectbox(
            "Sort by", ["none", "units", "priority", "created_at", "status"], key="filter_sort"
        )
        filter_sort_order = st.radio(
            "Order", ["asc", "desc"], horizontal=True, key="filter_sort_order"
        )

    active_sort_by = filter_sort if filter_sort != "none" else None
    active_depot = filter_depot if filter_depot != "(all)" else None
    try:
        orders = get_orders(
            status=filter_status if filter_status != "all" else None,
            priority=filter_priority if filter_priority != "all" else None,
            address=filter_address.strip() if filter_address.strip() else None,
            units_min=filter_units_min if filter_units_min > 0 else None,
            units_max=filter_units_max if filter_units_max > 0 else None,
            depot_id=active_depot,
            sort_by=active_sort_by,
            sort_order=filter_sort_order if active_sort_by else None,
        )
        if orders:
            st.caption(f"{len(orders)} order(s) matched")
            st.dataframe(pd.DataFrame(orders), use_container_width=True)
        else:
            st.info("No orders match the current filters.")
    except RuntimeError as e:
        st.warning(str(e))

    st.divider()

    # ── Fleet & Inventory ──────────────────────────────────────────────────────
    st.subheader("Fleet & Inventory")
    try:
        _vehicles = get_vehicles()
        _depots   = get_depots()
    except Exception:
        _vehicles, _depots = [], []

    if not _vehicles and not _depots:
        st.info("No fleet or inventory data loaded. Upload Vehicles and Inventory CSVs above.")
    else:
        total_vehicle_capacity = sum(v.get("capacity_units", 0) for v in _vehicles)
        total_inventory_units  = sum(d.get("units_available", 0) for d in _depots)

        m1, m2, m3 = st.columns(3)
        m1.metric("Vehicles", len(_vehicles))
        m2.metric("Total Vehicle Capacity", total_vehicle_capacity)
        m3.metric("Total Inventory Units", total_inventory_units)

        fi_left, fi_right = st.columns(2)
        with fi_left:
            st.caption("Depots / Warehouses")
            if _depots:
                st.dataframe(pd.DataFrame(_depots), use_container_width=True, hide_index=True)
            else:
                st.info("No inventory loaded.")
        with fi_right:
            st.caption("Vehicles")
            if _vehicles:
                st.dataframe(pd.DataFrame(_vehicles), use_container_width=True, hide_index=True)
            else:
                st.info("No vehicles loaded.")

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
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Orders Fulfilled", kpis.get("orders_fulfilled", 0))
            k2.metric("Orders Skipped", kpis.get("orders_skipped", 0))
            k3.metric("Cross-Depot Splits", kpis.get("cross_depot_splits", 0))
            k4.metric("Avg Utilization", f"{kpis.get('avg_utilization_pct', 0)}%")

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

            skipped = plan.get("skipped_orders", [])
            if skipped:
                with st.expander(f"Skipped Orders ({len(skipped)})", expanded=True):
                    st.dataframe(
                        pd.DataFrame(skipped)[["order_id", "address", "units", "priority", "reason"]],
                        use_container_width=True,
                    )
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
                    if "pending_filter" in result:
                        st.session_state["_pending_filter"] = result["pending_filter"]
                    if "pending_plan" in result:
                        st.session_state["current_plan"] = result["pending_plan"]
                        st.session_state["_switch_to_plan_tab"] = True
                except Exception as e:
                    st.error(str(e))
            st.rerun()
