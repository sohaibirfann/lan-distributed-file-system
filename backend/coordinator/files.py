from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.orm import Session

from coordinator.auth import require_session
from coordinator.db import get_db
from coordinator.events import record_event
from coordinator.models import Account, Chunk, ChunkPlacement, File, Node
from coordinator.replication import _default_node_client
from coordinator.schemas import (
    ChunkDetailOut,
    ChunkPlacementOut,
    ChunkUnavailableReport,
    FileCreateRequest,
    FileDetailOut,
    FileOut,
    FileRenameRequest,
)
from coordinator.settings import MAX_FILE_SIZE_BYTES_KEY, get_required_setting

logger = logging.getLogger(__name__)

router = APIRouter()


def _file_out(db: Session, file: File) -> FileOut:
    chunk_count = db.query(Chunk).filter(Chunk.file_id == file.id).count()
    return FileOut(
        id=file.id,
        name=file.name,
        size_bytes=file.size_bytes,
        uploader_account_id=file.uploader_account_id,
        created_at=file.created_at,
        updated_at=file.updated_at,
        chunk_count=chunk_count,
    )


@router.post("/files", response_model=FileOut, status_code=201)
def create_file(
    body: FileCreateRequest,
    account: Account = Depends(require_session),
    db: Session = Depends(get_db),
) -> FileOut:
    sequence_indices = sorted(chunk.sequence_index for chunk in body.chunks)
    if sequence_indices != list(range(len(body.chunks))):
        raise HTTPException(
            status_code=422, detail="chunk sequence_index values must be exactly 0..N-1"
        )

    declared_total = sum(chunk.size_bytes for chunk in body.chunks)
    if declared_total != body.size_bytes:
        raise HTTPException(
            status_code=422,
            detail=f"size_bytes ({body.size_bytes}) does not match the sum of chunk sizes ({declared_total})",
        )

    max_file_size_bytes = int(get_required_setting(db, MAX_FILE_SIZE_BYTES_KEY))
    if body.size_bytes > max_file_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds the maximum allowed size ({max_file_size_bytes} bytes)",
        )

    node_ids = {node_id for chunk in body.chunks for node_id in chunk.node_ids}
    existing_node_ids = {
        row.id for row in db.query(Node.id).filter(Node.id.in_(node_ids)).all()
    }
    missing = node_ids - existing_node_ids
    if missing:
        raise HTTPException(status_code=422, detail=f"unknown node_ids: {sorted(missing)}")

    file = File(name=body.name, size_bytes=body.size_bytes, uploader_account_id=account.id)
    db.add(file)
    db.flush()  # assigns file.id for the chunks below, without committing yet

    for chunk_in in body.chunks:
        chunk = Chunk(
            file_id=file.id,
            sequence_index=chunk_in.sequence_index,
            hash=chunk_in.hash,
            size_bytes=chunk_in.size_bytes,
        )
        db.add(chunk)
        db.flush()  # assigns chunk.id for the placements below
        for node_id in chunk_in.node_ids:
            db.add(ChunkPlacement(chunk_id=chunk.id, node_id=node_id))

    db.commit()
    db.refresh(file)
    return _file_out(db, file)


@router.get("/files", response_model=list[FileOut])
def list_files(
    account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> list[FileOut]:
    files = db.query(File).order_by(File.id).all()
    return [_file_out(db, file) for file in files]


@router.get("/files/{file_id}", response_model=FileDetailOut)
def get_file(
    file_id: int, account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> FileDetailOut:
    file = db.get(File, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="file not found")

    chunks = (
        db.query(Chunk).filter(Chunk.file_id == file_id).order_by(Chunk.sequence_index).all()
    )
    chunk_details = []
    for chunk in chunks:
        placements = (
            db.query(ChunkPlacement, Node)
            .join(Node, ChunkPlacement.node_id == Node.id)
            .filter(ChunkPlacement.chunk_id == chunk.id)
            .all()
        )
        chunk_details.append(
            ChunkDetailOut(
                sequence_index=chunk.sequence_index,
                hash=chunk.hash,
                size_bytes=chunk.size_bytes,
                nodes=[
                    ChunkPlacementOut(node_id=node.id, address=node.address)
                    for _, node in placements
                ],
            )
        )

    return FileDetailOut(
        id=file.id,
        name=file.name,
        size_bytes=file.size_bytes,
        uploader_account_id=file.uploader_account_id,
        created_at=file.created_at,
        updated_at=file.updated_at,
        chunks=chunk_details,
    )


@router.patch("/files/{file_id}", response_model=FileOut)
def rename_file(
    file_id: int,
    body: FileRenameRequest,
    account: Account = Depends(require_session),
    db: Session = Depends(get_db),
) -> FileOut:
    file = db.get(File, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="file not found")

    file.name = body.name
    file.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(file)
    return _file_out(db, file)


@router.delete("/files/{file_id}", status_code=204)
def delete_file(
    file_id: int, account: Account = Depends(require_session), db: Session = Depends(get_db)
) -> None:
    file = db.get(File, file_id)
    if file is None:
        raise HTTPException(status_code=404, detail="file not found")

    # Chunks aren't deduplicated across files, so node_id is kept alongside
    # address for the post-commit check below to tell if another file still needs it.
    to_reclaim = (
        db.query(Chunk.hash, Node.id, Node.address)
        .join(ChunkPlacement, ChunkPlacement.chunk_id == Chunk.id)
        .join(Node, ChunkPlacement.node_id == Node.id)
        .filter(Chunk.file_id == file_id)
        .all()
    )

    chunk_ids = [row.id for row in db.query(Chunk.id).filter(Chunk.file_id == file_id).all()]
    if chunk_ids:
        db.query(ChunkPlacement).filter(ChunkPlacement.chunk_id.in_(chunk_ids)).delete(
            synchronize_session=False
        )
        db.query(Chunk).filter(Chunk.file_id == file_id).delete(synchronize_session=False)

    db.delete(file)
    db.commit()

    # Best-effort: an unreachable node keeps the bytes until the gc sweep catches up.
    for chunk_hash, node_id, address in to_reclaim:
        # This file's own rows are already gone, so any remaining match here
        # belongs to a different file that still needs this exact blob.
        still_needed = (
            db.query(ChunkPlacement)
            .join(Chunk, ChunkPlacement.chunk_id == Chunk.id)
            .filter(Chunk.hash == chunk_hash, ChunkPlacement.node_id == node_id)
            .first()
            is not None
        )
        if still_needed:
            continue

        try:
            _default_node_client(address).delete(f"/chunks/{chunk_hash}")
        except httpx.HTTPError as err:
            logger.warning("could not reclaim chunk %s from %s: %s", chunk_hash, address, err)


@router.post("/chunks/{chunk_id}/report-unavailable", status_code=204)
def report_chunk_unavailable(
    body: ChunkUnavailableReport,
    chunk_id: str = Path(pattern=r"^[0-9a-f]{64}$"),
    account: Account = Depends(require_session),
    db: Session = Depends(get_db),
) -> None:
    if db.get(Node, body.node_id) is None:
        raise HTTPException(status_code=404, detail="node not found")
    record_event(
        db, "chunk_unavailable", f"chunk {chunk_id} unreachable on node {body.node_id}"
    )
    db.commit()
