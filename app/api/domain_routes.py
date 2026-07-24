"""Authenticated wishlist and long-term-memory endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.api.errors import ApiError
from app.api.schemas import (
    AddWishlistItemRequest,
    CreateMemoryRequest,
    CreateMemorySkillRequest,
    ConfirmMemoryCandidatesRequest,
    UpdateMemoryRequest,
    UpdateMemorySkillRequest,
    UpdateWishlistItemRequest,
)
from app.auth.dependencies import csrf_user, current_user
from app.auth.service import Principal
from app.database.services import MemoryService, WishlistService
from app.products.price_worker import PriceRefreshWorker

router = APIRouter(tags=["user-data"])


def wishlist_service() -> WishlistService:
    raise RuntimeError("wishlist_service dependency must be overridden by create_app")


def memory_service() -> MemoryService:
    raise RuntimeError("memory_service dependency must be overridden by create_app")


def price_refresh_worker() -> PriceRefreshWorker:
    raise RuntimeError("price_refresh_worker dependency must be overridden by create_app")


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


@router.post("/wishlists/default/items/{item_id}/refresh")
async def refresh_wishlist_price(
    item_id: str,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[WishlistService, Depends(wishlist_service)],
    worker: Annotated[PriceRefreshWorker, Depends(price_refresh_worker)],
) -> dict:
    await service.history(principal.user_id, item_id)
    return await worker.refresh_item(item_id)


@router.get("/memories")
async def list_memories(
    principal: Annotated[Principal, Depends(current_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
) -> dict:
    return {"items": await service.list(principal.user_id)}


@router.get("/memory-skills")
async def list_memory_skills(
    principal: Annotated[Principal, Depends(current_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
) -> dict:
    return {"items": await service.list_skills(principal.user_id)}


@router.post("/memory-skills", status_code=201)
async def create_memory_skill(
    payload: CreateMemorySkillRequest,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
) -> dict:
    return await service.create_skill(principal.user_id, **payload.model_dump())


@router.patch("/memory-skills/{skill_id}")
async def update_memory_skill(
    skill_id: str,
    payload: UpdateMemorySkillRequest,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
) -> dict:
    return await service.update_skill(principal.user_id, skill_id, **payload.model_dump(exclude_none=True))


@router.delete("/memory-skills/{skill_id}", status_code=204)
async def delete_memory_skill(
    skill_id: str,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
) -> Response:
    await service.delete_skill(principal.user_id, skill_id)
    return Response(status_code=204)


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
        skill_id=payload.skill_id,
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
        key=payload.key,
        skill_id=payload.skill_id,
    )


@router.post("/memories/confirm", status_code=201)
async def confirm_memory_candidates(
    payload: ConfirmMemoryCandidatesRequest,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
) -> dict:
    created = []
    conflicts = []
    for item in payload.items:
        try:
            created.append(await service.create(
                principal.user_id, **item.model_dump(), source_thread_id=payload.source_thread_id,
                source_run_id=payload.source_run_id, source="agent_confirmed"
            ))
        except ApiError as exc:
            if exc.code == "MEMORY_KEY_EXISTS":
                conflicts.append({"key": item.key, "code": exc.code, "message": exc.message})
            else:
                raise
    return {"items": created, "conflicts": conflicts}


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: str,
    principal: Annotated[Principal, Depends(csrf_user)],
    service: Annotated[MemoryService, Depends(memory_service)],
) -> Response:
    await service.delete(principal.user_id, memory_id)
    return Response(status_code=204)
