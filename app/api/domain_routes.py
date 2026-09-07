"""Authenticated wishlist endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.schemas import (
    AddWishlistItemRequest,
    UpdateWishlistItemRequest,
)
from app.auth.dependencies import csrf_user, current_user
from app.auth.service import Principal
from app.database.services import WishlistService

router = APIRouter(tags=["user-data"])


def wishlist_service() -> WishlistService:
    raise RuntimeError("wishlist_service dependency must be overridden by create_app")


@router.get("/wishlists/default")
async def get_default_wishlist(
    principal: Annotated[Principal, Depends(current_user)],
    service: Annotated[WishlistService, Depends(wishlist_service)],
) -> dict:
    return await service.get_default(principal.user_id)


@router.post("/wishlists/default/items", status_code=201)
async def add_wishlist_item(
    payload: AddWishlistItemRequest,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[WishlistService, Depends(wishlist_service)],
) -> dict:
    return await service.add(
        principal.user_id,
        offer_id=payload.offer_id,
        source_thread_id=payload.source_thread_id,
        source_run_id=payload.source_run_id,
        client_request_id=payload.client_request_id,
    )


@router.patch("/wishlists/default/items/{item_id}")
async def update_wishlist_item(
    item_id: str,
    payload: UpdateWishlistItemRequest,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[WishlistService, Depends(wishlist_service)],
) -> dict:
    fields = payload.model_fields_set
    return await service.update_item(
        principal.user_id,
        item_id,
        status=payload.status,
        target_price=payload.target_price,
        note=payload.note,
        target_price_set="target_price" in fields,
        note_set="note" in fields,
    )


@router.delete("/wishlists/default/items/{item_id}", status_code=204)
async def delete_wishlist_item(
    item_id: str,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[WishlistService, Depends(wishlist_service)],
) -> Response:
    await service.remove(principal.user_id, item_id)
    return Response(status_code=204)


@router.get("/wishlists/default/items/{item_id}/price-history")
async def wishlist_price_history(
    item_id: str,
    principal: Annotated[Principal, Depends(current_user)],
    service: Annotated[WishlistService, Depends(wishlist_service)],
) -> dict:
    return await service.history(principal.user_id, item_id)
