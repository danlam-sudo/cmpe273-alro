import asyncio
import osmnx as ox
from graph import G, snap_to_node


def geocode_address(address: str) -> tuple[float, float]:
    """
    Converts an address string to snapped road-graph coordinates.
    Appends ", San Jose, CA" if not already present to bias Nominatim results.
    Returns (lat, lon) of the nearest road node.
    """
    if "san jose" not in address.lower() and "ca" not in address.lower():
        address = f"{address}, San Jose, CA"

    lat, lon = ox.geocode(address)   # Nominatim via OSMnx — synchronous HTTP call
    node = snap_to_node(lat, lon)
    return G.nodes[node]["y"], G.nodes[node]["x"]


async def geocode_async(address: str) -> tuple[float, float]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, geocode_address, address)
