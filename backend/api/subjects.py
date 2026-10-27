"""Subjects routes: the owner's subjects/courses (CRUD) and assigning documents to them.

Organizational grouping only. Deleting a subject unassigns its documents; it never deletes a
document or any of its Chat, Summary, Flashcards, Quiz or Planner data.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import subject_store
from backend.api.deps import require_current_user

router = APIRouter()


class SubjectResponse(BaseModel):
    subject_id: str
    name: str
    color: str
    created_at: str
    updated_at: str
    document_ids: list[str] = Field(default_factory=list)
    document_count: int = 0


class SubjectCreateRequest(BaseModel):
    name: str = Field(max_length=200)
    color: Optional[str] = None


class SubjectUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    color: Optional[str] = None


class SubjectAssignRequest(BaseModel):
    document_ids: list[str] = Field(min_length=1, max_length=500)
    subject_id: Optional[str] = None   # None = Unassigned


class SubjectAssignResponse(BaseModel):
    subject_id: Optional[str]
    document_ids: list[str]
    subjects: list[SubjectResponse]


class SubjectDeleteResponse(BaseModel):
    deleted: str
    unassigned_document_ids: list[str]
    subjects: list[SubjectResponse]


def _subjects(owner_id: str) -> list[SubjectResponse]:
    return [SubjectResponse(**subject) for subject in subject_store.list_subjects(owner_id)]


@router.get("/api/subjects", response_model=list[SubjectResponse])
def list_subjects(current_user: dict = Depends(require_current_user)) -> list[SubjectResponse]:
    return _subjects(current_user["id"])


@router.post("/api/subjects", response_model=SubjectResponse, status_code=201)
def create_subject(request: SubjectCreateRequest, current_user: dict = Depends(require_current_user)) -> SubjectResponse:
    try:
        return SubjectResponse(**subject_store.create_subject(current_user["id"], request.name, request.color))
    except subject_store.SubjectNameConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.patch("/api/subjects/{subject_id}", response_model=SubjectResponse)
def update_subject(
    subject_id: str, request: SubjectUpdateRequest, current_user: dict = Depends(require_current_user),
) -> SubjectResponse:
    try:
        return SubjectResponse(**subject_store.update_subject(current_user["id"], subject_id, request.name, request.color))
    except subject_store.SubjectNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except subject_store.SubjectNameConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.delete("/api/subjects/{subject_id}", response_model=SubjectDeleteResponse)
def delete_subject(subject_id: str, current_user: dict = Depends(require_current_user)) -> SubjectDeleteResponse:
    """Delete the subject only: its documents become Unassigned and keep all their data."""
    try:
        result = subject_store.delete_subject(current_user["id"], subject_id)
    except subject_store.SubjectNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return SubjectDeleteResponse(**result, subjects=_subjects(current_user["id"]))


@router.post("/api/subjects/assign", response_model=SubjectAssignResponse)
def assign_documents(request: SubjectAssignRequest, current_user: dict = Depends(require_current_user)) -> SubjectAssignResponse:
    """Assign, move or (subject_id null) unassign one or more of the owner's documents."""
    try:
        updated = subject_store.assign_documents(current_user["id"], request.document_ids, request.subject_id)
    except (subject_store.SubjectNotFound, subject_store.DocumentNotFound) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return SubjectAssignResponse(subject_id=request.subject_id, document_ids=updated, subjects=_subjects(current_user["id"]))
