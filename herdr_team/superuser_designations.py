"""Superuser designation registry for W-16 Designated Superuser Pane/Session feature.

This module handles the storage and management of server-side superuser designations
for the designated superuser pane/session feature. The registry is stored in
sessions/<slug>/superuser_designations.json and contains entries that map
pane_id or session_id to superuser designation records.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from herdr_team.paths import SessionPaths
from herdr_team import store
from herdr_team.errors import HerdrTeamError


@dataclass
class DesignationEntry:
    pane_id: Optional[str] = None
    session_id: Optional[str] = None
    designated_by: str = ""
    designated_at: str = ""
    revoked_at: Optional[str] = None
    source: str = ""

    @property
    def active(self) -> bool:
        """Check if this designation is still active (not revoked)."""
        return self.revoked_at is None


@dataclass
class DesignationRegistry:
    session: SessionPaths

    def load(self) -> List[DesignationEntry]:
        """Load the current designations from disk."""
        try:
            doc = store.read_json(self.session.superuser_designations_json, default=None)
            if not isinstance(doc, dict):
                return []
            entries = doc.get("entries")
            if not isinstance(entries, list):
                return []
            loaded: List[DesignationEntry] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                loaded.append(DesignationEntry(
                    pane_id=entry.get("pane_id"),
                    session_id=entry.get("session_id"),
                    designated_by=entry.get("designated_by", ""),
                    designated_at=entry.get("designated_at", ""),
                    revoked_at=entry.get("revoked_at"),
                    source=entry.get("source", ""),
                ))
            return loaded
        except Exception:
            # Return empty list on any error (file doesn't exist, malformed, etc.)
            return []

    def save(self, entries: List[DesignationEntry]) -> None:
        """Save the current designations to disk."""
        doc = {
            "v": 1,
            "entries": [entry.__dict__ for entry in entries]
        }
        store.write_json(self.session.superuser_designations_json, doc)

    def find_active(self, pane_id: Optional[str] = None, session_id: Optional[str] = None) -> List[DesignationEntry]:
        """Find all active (non-revoked) designation entries matching the criteria."""
        entries = self.load()
        active_entries = [e for e in entries if e.active]
        if pane_id:
            active_entries = [e for e in active_entries if e.pane_id == pane_id]
        if session_id:
            active_entries = [e for e in active_entries if e.session_id == session_id]
        return active_entries

    def is_designated(self, pane_id: Optional[str] = None, session_id: Optional[str] = None) -> bool:
        """Check if there's an active designation for the given pane_id or session_id."""
        return bool(self.find_active(pane_id, session_id))

    def designate(self, pane_id: Optional[str] = None, session_id: Optional[str] = None, 
                 team: str = "", source: str = "") -> None:
        """Create a new active designation entry.

        Idempotent: an existing ACTIVE entry for the same pane or session is
        left alone rather than duplicated (W-16 design §7).
        """
        entries = self.load()
        for entry in entries:
            if not entry.active:
                continue
            if pane_id and entry.pane_id == pane_id:
                return
            if session_id and entry.session_id == session_id:
                return
        new_entry = DesignationEntry(
            pane_id=pane_id,
            session_id=session_id,
            designated_by=team,
            designated_at=store.now_iso(),
            revoked_at=None,
            source=source
        )
        entries.append(new_entry)
        self.save(entries)

    def revoke(self, pane_id: Optional[str] = None, session_id: Optional[str] = None) -> bool:
        """Revoke an existing designation entry. Returns True if successful."""
        entries = self.load()
        for entry in entries:
            if not entry.active:
                continue
            if pane_id and entry.pane_id == pane_id:
                entry.revoked_at = store.now_iso()
                self.save(entries)
                return True
            if session_id and entry.session_id == session_id:
                entry.revoked_at = store.now_iso()
                self.save(entries)
                return True
        return False

    def list_designations(self) -> List[Dict[str, Any]]:
        """Return a list of designation entries for display purposes."""
        entries = self.load()
        result = []
        for entry in entries:
            if not entry.active:
                continue
            result.append({
                "pane_id": entry.pane_id,
                "session_id": entry.session_id,
                "designated_by": entry.designated_by,
                "designated_at": entry.designated_at,
                "revoked_at": entry.revoked_at,
                "source": entry.source,
                "active": True
            })
        return result