"""CRUD for trade_notes (DESIGN §8 trade_notes table, USER_STORIES Story 6)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from stockscan.db import session_scope

NoteType = Literal["entry", "mid", "exit", "free"]


@dataclass(frozen=True, slots=True)
class Note:
    note_id: int
    trade_id: int
    created_at: datetime
    updated_at: datetime
    note_type: NoteType
    body: str
    template_fields: dict[str, Any] | None


def _row_to_note(r: Any) -> Note:
    fields = r.template_fields
    if isinstance(fields, str):
        fields = json.loads(fields)
    return Note(
        note_id=int(r.note_id),
        trade_id=int(r.trade_id),
        created_at=r.created_at,
        updated_at=r.updated_at,
        note_type=r.note_type,
        body=r.body,
        template_fields=fields,
    )


def create_note(
    trade_id: int,
    body: str,
    note_type: NoteType = "free",
    template_fields: dict[str, Any] | None = None,
    *,
    session: Session | None = None,
) -> Note:
    sql = text(
        """
        INSERT INTO trade_notes (trade_id, note_type, body, template_fields)
        VALUES (:tid, :nt, :body, CAST(:fields AS JSONB))
        RETURNING note_id, trade_id, created_at, updated_at, note_type, body, template_fields;
        """
    )
    params = {
        "tid": trade_id,
        "nt": note_type,
        "body": body,
        "fields": json.dumps(template_fields) if template_fields else None,
    }

    def _run(s: Session) -> Note:
        return _row_to_note(s.execute(sql, params).one())

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def list_notes_for_trade(trade_id: int, *, session: Session | None = None) -> list[Note]:
    sql = text(
        "SELECT note_id, trade_id, created_at, updated_at, note_type, body, template_fields "
        "FROM trade_notes WHERE trade_id = :tid ORDER BY created_at"
    )

    def _run(s: Session) -> list[Note]:
        return [_row_to_note(r) for r in s.execute(sql, {"tid": trade_id})]

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def update_note(
    note_id: int,
    body: str,
    template_fields: dict[str, Any] | None = None,
    *,
    session: Session | None = None,
) -> Note:
    """Update a note. Captures the previous body in trade_note_revisions for audit."""
    revision_sql = text(
        """
        INSERT INTO trade_note_revisions (note_id, body_before, template_fields_before)
        SELECT note_id, body, template_fields FROM trade_notes WHERE note_id = :nid;
        """
    )
    update_sql = text(
        """
        UPDATE trade_notes
        SET body = :body,
            template_fields = CAST(:fields AS JSONB),
            updated_at = NOW()
        WHERE note_id = :nid
        RETURNING note_id, trade_id, created_at, updated_at, note_type, body, template_fields;
        """
    )

    def _run(s: Session) -> Note:
        s.execute(revision_sql, {"nid": note_id})
        return _row_to_note(
            s.execute(
                update_sql,
                {
                    "nid": note_id,
                    "body": body,
                    "fields": json.dumps(template_fields) if template_fields else None,
                },
            ).one()
        )

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)


def delete_note(note_id: int, *, session: Session | None = None) -> None:
    sql = text("DELETE FROM trade_notes WHERE note_id = :nid")

    def _run(s: Session) -> None:
        s.execute(sql, {"nid": note_id})

    if session is not None:
        _run(session)
        return
    with session_scope() as s:
        _run(s)


def search_notes(
    query: str,
    *,
    limit: int = 50,
    session: Session | None = None,
) -> list[tuple[Note, str]]:
    """Postgres full-text search across notes. Returns (note, snippet) tuples."""
    sql = text(
        """
        SELECT note_id, trade_id, created_at, updated_at, note_type, body, template_fields,
               ts_headline('english', body, plainto_tsquery('english', :q),
                           'MaxFragments=2,MaxWords=15,MinWords=5') AS snippet
        FROM trade_notes
        WHERE body_tsv @@ plainto_tsquery('english', :q)
        ORDER BY ts_rank(body_tsv, plainto_tsquery('english', :q)) DESC
        LIMIT :lim;
        """
    )

    def _run(s: Session) -> list[tuple[Note, str]]:
        out: list[tuple[Note, str]] = []
        for r in s.execute(sql, {"q": query, "lim": limit}):
            out.append((_row_to_note(r), r.snippet))
        return out

    if session is not None:
        return _run(session)
    with session_scope() as s:
        return _run(s)
