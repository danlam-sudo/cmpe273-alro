import os
import sys

# Make service modules importable without packaging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../solver-service"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../planner-service"))


def minimal_matrix(n: int, off_diag: float = 10.0) -> list[list[float]]:
    """n×n distance matrix: 0 on diagonal, off_diag everywhere else."""
    return [[0.0 if i == j else off_diag for j in range(n)] for i in range(n)]
