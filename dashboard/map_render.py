import folium

VEHICLE_COLORS = ["red", "blue", "green", "purple", "orange",
                  "darkred", "cadetblue", "darkgreen"]


def build_map(plan: dict, depots: list[dict]) -> folium.Map:
    m = folium.Map(location=[37.3382, -121.8863], zoom_start=12, tiles="OpenStreetMap")

    for depot in depots:
        folium.Marker(
            location=[depot["lat"], depot["lon"]],
            tooltip=depot["warehouse_id"],
            icon=folium.Icon(icon="home", prefix="fa", color="black"),
        ).add_to(m)

    for i, route in enumerate(plan.get("routes", [])):
        color = VEHICLE_COLORS[i % len(VEHICLE_COLORS)]
        for stop in route["stops"]:
            if stop.get("road_geometry"):
                folium.PolyLine(
                    stop["road_geometry"],
                    color=color, weight=3, opacity=0.8,
                ).add_to(m)
            folium.CircleMarker(
                location=[stop["lat"], stop["lon"]],
                radius=7, color=color, fill=True, fill_opacity=0.9,
                tooltip=f"Truck {i + 1} | {stop['sub_order_id'][:8]}",
            ).add_to(m)

    return m


def render_map(plan: dict, depots: list[dict]) -> str:
    """Returns HTML string for st.components.v1.html()."""
    return build_map(plan, depots)._repr_html_()
