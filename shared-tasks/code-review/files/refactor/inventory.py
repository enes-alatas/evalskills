def low_stock(items: list[dict], threshold: int) -> list[str]:
    """SKUs whose count is below the threshold, in input order."""
    return [item["sku"] for item in items if item["count"] < threshold]


def restock_cost(items: list[dict], unit_costs: dict[str, float]) -> float:
    """Cost of buying every missing unit at its unit cost."""
    return sum(unit_costs[item["sku"]] * item["missing"] for item in items)
