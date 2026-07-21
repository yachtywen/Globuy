"""Domestic single-offer shipping and total-cost calculation."""

from typing import Annotated, Any, Literal

from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from app.tools.costs import calculate_domestic_cost


class ShippingCalcInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_price: Annotated[float, Field(ge=0)]
    quantity: Annotated[int, Field(ge=1)] = 1
    shipping_fee: Annotated[float | None, Field(ge=0)] = None
    currency: Literal["CNY"] = "CNY"


@tool(args_schema=ShippingCalcInput)
def shipping_calc(
    item_price: Annotated[float, Field(ge=0)],
    quantity: Annotated[int, Field(ge=1)] = 1,
    shipping_fee: Annotated[float | None, Field(ge=0)] = None,
    currency: Literal["CNY"] = "CNY",
) -> dict[str, Any]:
    """Calculate one domestic offer; unknown shipping remains unknown."""

    return calculate_domestic_cost(
        item_price=item_price,
        quantity=quantity,
        shipping_fee=shipping_fee,
        currency=currency,
    )
