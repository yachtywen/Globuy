"""Authenticated wishlist and long-term-memory endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.api.schemas import (
    AddWishlistItemRequest,
    ConfirmMemoryCandidateRequest,
    CreateMemoryRequest,
    UpdateMemoryRequest,
    UpdateWishlistItemRequest,
)
from app.auth.dependencies import csrf_user, current_user
from app.auth.service import Principal
from app.database.services import MemoryService, WishlistService

router = APIRouter(tags=["user-data"])


def wishlist_service() -> WishlistService:
    raise RuntimeError("wishlist_service dependency must be overridden by create_app")


def memory_service() -> MemoryService:
    raise RuntimeError("memory_service dependency must be overridden by create_app")


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


@router.get("/memories")
async def list_memories(
    principal: Annotated[Principal, Depends(current_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
    status: Annotated[str, Query(pattern="^(active|archived)$")] = "active",
) -> dict:
    return {"items": await service.list(principal.user_id, lifecycle_status=status)}


@router.post("/memories", status_code=201)
async def create_memory(
    payload: CreateMemoryRequest,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
) -> dict:
    return await service.create(
        principal.user_id,
        category=payload.category,
        key=payload.key,
        content=payload.content,
        confidence=payload.confidence,
        source_thread_id=payload.source_thread_id,
        source_run_id=payload.source_run_id,
    )


@router.patch("/memories/{memory_id}")
async def update_memory(
    memory_id: str,
    payload: UpdateMemoryRequest,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
) -> dict:
    return await service.update(
        principal.user_id,
        memory_id,
        category=payload.category,
        content=payload.content,
        confidence=payload.confidence,
    )


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: str,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
) -> Response:
    await service.delete(principal.user_id, memory_id)
    return Response(status_code=204)


@router.post("/memories/{memory_id}/restore")
async def restore_memory(
    memory_id: str,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
) -> dict:
    return await service.restore(principal.user_id, memory_id)


@router.get("/memory-candidates")
async def list_memory_candidates(
    principal: Annotated[Principal, Depends(current_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
    status: Annotated[str, Query(pattern="^(pending|confirmed|rejected|expired)$")] = "pending",
) -> dict:
    return {"items": await service.list_candidates(principal.user_id, status=status)}


@router.post("/memory-candidates/{candidate_id}/confirm")
async def confirm_memory_candidate(
    candidate_id: str,
    payload: ConfirmMemoryCandidateRequest,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
) -> dict:
    return await service.confirm_candidate(
        principal.user_id,
        candidate_id,
        category=payload.category,
        key=payload.key,
        content=payload.content,
    )


@router.post("/memory-candidates/{candidate_id}/reject", status_code=204)
async def reject_memory_candidate(
    candidate_id: str,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
) -> Response:
    await service.reject_candidate(principal.user_id, candidate_id)
    return Response(status_code=204)
