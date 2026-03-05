import functools
import json
import os
from typing import Optional

import gkeepapi
import gkeepapi.node

from clam_ptb.tools.tool_spec import ArgSpec, LocalTool, ToolResult, ToolSpec

HIDDEN_LABEL = "hidden"


@functools.cache
def get_gkeep() -> gkeepapi.Keep:
    keep = gkeepapi.Keep()
    master_token = os.environ["MASTER_TOKEN"]
    email = os.environ["KEEP_EMAIL"]
    keep.authenticate(email, master_token)
    return keep


def dump_memory(memory: str):
    keep = get_gkeep()
    if not list(keep.find("MEMORY_DUMP", archived=False)):
        note = keep.createNote("MEMORY_DUMP", "")
        label_obj = keep.findLabel(HIDDEN_LABEL)
        if not label_obj:
            label_obj = keep.createLabel(HIDDEN_LABEL)

        assert isinstance(note, gkeepapi.node.TopLevelNode)

        note.labels.add(label_obj)
        keep.sync()
    # TODO: figure out why label matching is not working
    # [note] = keep.find("MEMORY_DUMP", labels=[HIDDEN_LABEL])
    [note] = keep.find("MEMORY_DUMP", archived=False)
    note.text = memory
    keep.sync()


def load_memory() -> list[dict] | None:
    keep = get_gkeep()
    notes = keep.find("MEMORY_DUMP", archived=False)
    if not notes:
        return None
    [note] = notes
    data = json.loads(note.text)
    assert isinstance(data, list), data
    return data


class KeepBaseTool(LocalTool):
    @property
    def _keep(self) -> gkeepapi.Keep:
        return get_gkeep()


class GkeepCreateNoteTool(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_create_note",
            description="Creates a new note in Google Keep",
            args=[
                ArgSpec(
                    name="title",
                    type=str,
                    description="Title of the note",
                    is_required=True,
                ),
                ArgSpec(
                    name="content",
                    type=str,
                    description="Content of the note",
                    is_required=True,
                ),
                ArgSpec(
                    name="pinned",
                    type=bool,
                    description="Whether to pin the note",
                    is_required=False,
                ),
                ArgSpec(
                    name="color",
                    type=str,
                    description="Color of the note (Red, Blue, Green, Yellow, Gray, Brown, Purple, Teal, White)",
                    is_required=False,
                ),
            ],
        )

    async def _execute(
        self,
        *,
        title: str,
        content: str,
        pinned: bool = False,
        color: str | None = None,
        **kwargs,
    ) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        note = self._keep.createNote(title, content)
        assert isinstance(note, gkeepapi.node.TopLevelNode)
        note.pinned = pinned

        if color:
            try:
                color_value = getattr(gkeepapi.node.ColorValue, color.capitalize())
                note.color = color_value
            except AttributeError:
                return ToolResult.error(f"Invalid color: {color}")

        self._keep.sync()
        return ToolResult.success(_note_to_dict(note))


class GkeepCreateListTool(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_create_list",
            description="Creates a new list note in Google Keep",
            args=[
                ArgSpec(
                    name="title",
                    type=str,
                    description="Title of the list",
                    is_required=True,
                ),
            ],
        )

    async def _execute(self, *, title: str, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        list_note = self._keep.createList(title)
        self._keep.sync()
        return ToolResult.success({"note_id": list_note.id})


class GkeepGetListItemsTool(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_get_list_items",
            description="Get items of a list note in Google Keep. Return list of pairs (text, is_checked)",
            args=[
                ArgSpec(
                    name="note_id",
                    type=str,
                    description="ID of the list note",
                    is_required=True,
                ),
                ArgSpec(
                    name="include_checked",
                    type=bool,
                    description="Whether to include checked items or only show unchecked items",
                ),
            ],
        )

    async def _execute(
        self, *, note_id: str, include_checked: bool, **kwargs
    ) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        note = self._keep.get(note_id)
        assert isinstance(note, gkeepapi.node.List), f"{type(note)=} is not list"
        items = note.items
        if not include_checked:
            items = [item for item in items if not item.checked]
        return ToolResult.success(
            {"items": [(item.text, item.checked) for item in items]}
        )


class GkeepAddListItem(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_add_list_item",
            description="Add a new item to a list note in Google Keep",
            args=[
                ArgSpec(
                    name="note_id",
                    type=str,
                    description="ID of the list note",
                    is_required=True,
                ),
                ArgSpec(
                    name="text",
                    type=str,
                    description="Text of the item",
                    is_required=True,
                ),
            ],
        )

    async def _execute(self, *, note_id: str, text: str, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        note = self._keep.get(note_id)
        assert isinstance(note, gkeepapi.node.List), f"{type(note)=} is not list"
        note.add(text, False, gkeepapi.node.NewListItemPlacementValue.Top)
        self._keep.sync()
        return ToolResult.success("added as index 0")


class GkeepUpdateListItem(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_update_list_item",
            description="Change is_checked status of a list item in Google Keep. Optionally changes content",
            args=[
                ArgSpec(
                    name="note_id",
                    type=str,
                    description="ID of the list note",
                    is_required=True,
                ),
                ArgSpec(
                    name="item_index",
                    type=int,
                    description="Index of the item in the list",
                    is_required=True,
                ),
                ArgSpec(
                    name="is_checked",
                    type=bool,
                    description="Sets the checked status of the item to this value",
                    is_required=True,
                ),
                ArgSpec(
                    name="text",
                    type=str,
                    description="Optional new text for the item. Will not change if not provided",
                    is_required=False,
                ),
            ],
        )

    async def _execute(
        self, *, note_id: str, item_index: int, is_checked: bool, text: str, **kwargs
    ) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        note = self._keep.get(note_id)
        assert isinstance(note, gkeepapi.node.List), f"{type(note)=} is not list"
        items = note.items
        the_item = items[item_index]
        the_item.checked = is_checked
        if text:
            the_item.text = text
        self._keep.sync()
        return ToolResult.success("updated")


class GkeepDeleteListItem(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_delete_list_item",
            description="Delete a list item in Google Keep",
            args=[
                ArgSpec(
                    name="note_id",
                    type=str,
                    description="ID of the list note",
                    is_required=True,
                ),
                ArgSpec(
                    name="item_index",
                    type=int,
                    description="Index of the item in the list",
                    is_required=True,
                ),
            ],
        )

    async def _execute(self, *, note_id: str, item_index: int, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        note = self._keep.get(note_id)
        assert isinstance(note, gkeepapi.node.List), f"{type(note)=} is not list"
        items = note.items
        the_item = items[item_index]
        text = the_item.text
        the_item.delete()
        self._keep.sync()
        return ToolResult.success(f"deleted item {text!r}")


class GkeepGetNoteTool(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_get_note",
            description="Retrieves a note from Google Keep by ID",
            args=[
                ArgSpec(
                    name="note_id",
                    type=str,
                    description="ID of the note to retrieve",
                    is_required=True,
                ),
            ],
        )

    async def _execute(self, *, note_id: str, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        note = self._keep.get(note_id)
        if not note or _is_note_hidden(note):
            return ToolResult.error(f"Note not found: {note_id}")

        return ToolResult.success(_note_to_dict(note))


class GkeepUpdateNoteTool(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_update_note",
            description="Updates an existing note in Google Keep",
            args=[
                ArgSpec(
                    name="note_id",
                    type=str,
                    description="ID of the note to update",
                    is_required=True,
                ),
                ArgSpec(
                    name="title",
                    type=str,
                    description="New title of the note",
                    is_required=False,
                ),
                ArgSpec(
                    name="content",
                    type=str,
                    description="New content of the note",
                    is_required=False,
                ),
                ArgSpec(
                    name="pinned",
                    type=bool,
                    description="Whether to pin the note",
                    is_required=False,
                ),
                ArgSpec(
                    name="color",
                    type=str,
                    description="New color of the note",
                    is_required=False,
                ),
            ],
        )

    async def _execute(
        self,
        *,
        note_id: str,
        title: Optional[str] = None,
        content: Optional[str] = None,
        pinned: Optional[bool] = None,
        color: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        note = self._keep.get(note_id)
        if not note:
            return ToolResult.error(f"Note not found: {note_id}")

        if title is not None:
            note.title = title
        if content is not None:
            note.text = content
        if pinned is not None:
            note.pinned = pinned
        if color is not None:
            try:
                color_value = getattr(gkeepapi.node.ColorValue, color.capitalize())
                note.color = color_value
            except AttributeError:
                return ToolResult.error(f"Invalid color: {color}")

        self._keep.sync()
        return ToolResult.success(_note_to_dict(note))


class GkeepDeleteNoteTool(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_delete_note",
            description="Deletes a note from Google Keep",
            args=[
                ArgSpec(
                    name="note_id",
                    type=str,
                    description="ID of the note to delete",
                    is_required=True,
                ),
            ],
        )

    async def _execute(self, *, note_id: str, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        note = self._keep.get(note_id)
        if not note:
            return ToolResult.error(f"Note not found: {note_id}")

        note.delete()
        self._keep.sync()
        return ToolResult.success({"message": f"Note {note_id} deleted successfully"})


class GkeepListNotesByLabelTool(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_list_notes_by_label",
            description="Lists all notes with a specific label",
            args=[
                ArgSpec(
                    name="label",
                    type=str,
                    description="Label to filter notes by",
                    is_required=True,
                ),
            ],
        )

    async def _execute(self, *, label: str, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        if label == HIDDEN_LABEL:
            notes = []
        else:
            gnotes = self._keep.find(labels=[label])
            notes = [_note_to_dict(note) for note in gnotes]

        return ToolResult.success({"notes": notes, "count": len(notes)})


class GkeepAddLabelTool(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_add_label",
            description="Adds a label to a note",
            args=[
                ArgSpec(
                    name="note_id",
                    type=str,
                    description="ID of the note",
                    is_required=True,
                ),
                ArgSpec(
                    name="label",
                    type=str,
                    description="Label to add to the note",
                    is_required=True,
                ),
            ],
        )

    async def _execute(self, *, note_id: str, label: str, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        note = self._keep.get(note_id)
        if not note:
            return ToolResult.error(f"Note not found: {note_id}")

        # Create label if it doesn't exist
        label_obj = self._keep.findLabel(label)
        if not label_obj:
            label_obj = self._keep.createLabel(label)

        note.labels.add(label_obj)
        self._keep.sync()

        return ToolResult.success(
            {
                "id": note.id,
                "title": note.title,
                "labels": [lab.name for lab in note.labels.all()],
            }
        )


class GkeepRemoveLabelTool(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_remove_label",
            description="Removes a label from a note",
            args=[
                ArgSpec(
                    name="note_id",
                    type=str,
                    description="ID of the note",
                    is_required=True,
                ),
                ArgSpec(
                    name="label",
                    type=str,
                    description="Label to remove from the note",
                    is_required=True,
                ),
            ],
        )

    async def _execute(self, *, note_id: str, label: str, **kwargs) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"
        note = self._keep.get(note_id)
        if not note:
            return ToolResult.error(f"Note not found: {note_id}")

        label_obj = self._keep.findLabel(label)
        if not label_obj:
            return ToolResult.error(f"Label not found: {label}")

        note.labels.remove(label_obj)
        self._keep.sync()

        return ToolResult.success(
            {
                "id": note.id,
                "title": note.title,
                "labels": [x.name for x in note.labels.all()],
            }
        )


class GkeepListRecentNotesTool(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_list_recent_notes",
            description="Retrieves N most recent notes with optional filter for pinned notes",
            args=[
                ArgSpec(
                    name="limit",
                    type=int,
                    description="Number of recent notes to retrieve",
                    is_required=True,
                ),
                ArgSpec(
                    name="pinned_only",
                    type=bool,
                    description="If true, only return pinned notes",
                    is_required=False,
                ),
                ArgSpec(
                    name="allow_archived",
                    type=bool,
                    description="If true, will include archived notes; otherwise only non-arhived",
                    is_required=False,
                ),
            ],
        )

    async def _execute(
        self,
        *,
        limit: int,
        pinned_only: bool | None,
        allow_archived: bool | None,
        **kwargs,
    ) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"

        self._keep.sync()

        recent_notes = self._keep.all()
        recent_notes = [note for note in recent_notes if not note.trashed]
        recent_notes = [note for note in recent_notes if not _is_note_hidden(note)]
        if pinned_only:
            recent_notes = [note for note in recent_notes if note.pinned]
        if not allow_archived:
            recent_notes = [note for note in recent_notes if not note.archived]
        recent_notes = sorted(
            recent_notes, key=lambda x: x.timestamps.updated, reverse=True
        )

        notes_data = [
            _note_to_dict(note, truncate=True) for note in recent_notes[:limit]
        ]

        return ToolResult.success({"notes": notes_data, "count": len(notes_data)})


def _is_note_hidden(note: gkeepapi.node.TopLevelNode) -> bool:
    return HIDDEN_LABEL in [x.name for x in note.labels.all()]


def _note_to_dict(note: gkeepapi.node.TopLevelNode, truncate: bool = False) -> dict:
    content = note.text
    if truncate and len(content) > 100:
        content = content[:100] + f" ... ({len(content) - 100} more chars)"
    return {
        "note_id": note.id,
        "is_list": isinstance(note, gkeepapi.node.List),
        "title": note.title,
        "content": content,
        "pinned": note.pinned,
        "color": note.color.name if note.color else None,
        "labels": [x.name for x in note.labels.all()],
        "arhived": note.archived,
        "last_modified": note.timestamps.updated.isoformat()
        if note.timestamps.updated
        else None,
    }


class GkeepSearchNotesTool(KeepBaseTool):
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gkeep_search",
            description="Search notes in Google Keep by title or content",
            args=[
                ArgSpec(
                    name="query",
                    type=str,
                    description="Text to search for in notes",
                    is_required=True,
                ),
                ArgSpec(
                    name="include_archived",
                    type=bool,
                    description="Whether to include archived notes in search",
                    is_required=False,
                ),
            ],
        )

    async def _execute(
        self, *, query: str, include_archived: bool = False, **kwargs
    ) -> ToolResult:
        assert not kwargs, f"Unexpected kwargs: {kwargs}"

        self._keep.sync()
        # Search in both title and content
        notes = self._keep.find(query)

        # Filter out hidden and trashed notes
        notes = [note for note in notes if not _is_note_hidden(note)]
        notes = [note for note in notes if not note.trashed]

        if not include_archived:
            notes = [note for note in notes if not note.archived]

        # Sort by last modified
        notes = sorted(notes, key=lambda x: x.timestamps.updated, reverse=True)

        notes_data = [_note_to_dict(note) for note in notes]
        return ToolResult.success({"notes": notes_data, "count": len(notes_data)})


def get_gkeep_tools() -> list[LocalTool]:
    return [
        klass() for klass in KeepBaseTool.__subclasses__() if klass is not KeepBaseTool
    ]
