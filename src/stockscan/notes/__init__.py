"""Trade notes — markdown bodies, optional templated fields, FTS search."""

from stockscan.notes.store import (
    Note,
    NoteType,
    create_note,
    delete_note,
    list_notes_for_trade,
    search_notes,
    update_note,
)

__all__ = [
    "Note",
    "NoteType",
    "create_note",
    "delete_note",
    "list_notes_for_trade",
    "search_notes",
    "update_note",
]
