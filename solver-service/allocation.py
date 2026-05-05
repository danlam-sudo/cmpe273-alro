import uuid

import numpy as np
from scipy.optimize import linprog

from models import Depot, Order, SubOrder


def allocate(
    orders: list[Order],
    depots: list[Depot],
    cost_matrix: np.ndarray,   # shape (n_depots, n_orders), Haversine km
) -> list[SubOrder]:
    """
    Solves the Transportation LP to assign depot inventory to orders.
    Automatically splits orders that exceed any single depot's stock.

    Variables: x[i,j] = units shipped from depot i to order j
    Minimize:  sum(cost[i,j] * x[i,j])
    Subject to:
      sum_i(x[i,j]) == order[j].units   for all j  (demand met exactly)
      sum_j(x[i,j]) <= depot[i].stock   for all i  (supply not exceeded)
      x[i,j] >= 0
    """
    n_d, n_o = len(depots), len(orders)

    c = cost_matrix.flatten()

    # Demand equality: for each order j, sum over depots == units
    A_eq = np.zeros((n_o, n_d * n_o))
    b_eq = np.array([o.units for o in orders], dtype=float)
    for j in range(n_o):
        for i in range(n_d):
            A_eq[j, i * n_o + j] = 1.0

    # Supply inequality: for each depot i, sum over orders <= stock
    A_ub = np.zeros((n_d, n_d * n_o))
    b_ub = np.array([d.units_available for d in depots], dtype=float)
    for i in range(n_d):
        for j in range(n_o):
            A_ub[i, i * n_o + j] = 1.0

    bounds = [(0.0, None)] * (n_d * n_o)

    result = linprog(
        c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
        bounds=bounds, method="highs",
    )

    if not result.success:
        raise ValueError(
            f"Inventory allocation infeasible: {result.message}. "
            f"Total demand: {sum(o.units for o in orders)}, "
            f"Total supply: {sum(d.units_available for d in depots)}"
        )

    allocation = result.x.reshape(n_d, n_o)
    THRESHOLD = 0.5   # ignore allocations below half a unit (LP numerical noise)

    sub_orders = []
    for i, depot in enumerate(depots):
        for j, order in enumerate(orders):
            qty = allocation[i, j]
            if qty >= THRESHOLD:
                sub_orders.append(SubOrder(
                    sub_order_id=str(uuid.uuid4()),
                    parent_order_id=order.order_id,
                    depot_id=depot.warehouse_id,
                    delivery_lat=order.delivery_lat,
                    delivery_lon=order.delivery_lon,
                    units=round(qty),
                ))

    return sub_orders
