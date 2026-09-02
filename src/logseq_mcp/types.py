"""Pydantic models for Logseq API entities."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BlockRef(BaseModel):
    """Reference to a block — appears as either {"id": N} or bare N in API responses."""

    id: int

    @model_validator(mode="before")
    @classmethod
    def coerce_int(cls, v: Any) -> Any:
        if isinstance(v, int):
            return {"id": v}
        return v


class PageRef(BaseModel):
    """Reference to a page — appears as either {"id": N} or bare N in API responses."""

    id: int
    name: str = ""

    @model_validator(mode="before")
    @classmethod
    def coerce_int(cls, v: Any) -> Any:
        if isinstance(v, int):
            return {"id": v}
        return v


class PageEntity(BaseModel):
    """A Logseq page, as returned by getAllPages / getPage."""

    model_config = ConfigDict(populate_by_name=True)

    id: int = 0
    uuid: str = ""
    name: str = ""
    original_name: str = Field("", alias="original-name")
    journal: bool = Field(False, alias="journal?")
    journal_day: int | None = Field(None, alias="journal-day")
    properties: dict = Field(default_factory=dict)
    namespace: str = ""

    @model_validator(mode="before")
    @classmethod
    def coerce_namespace_ref(cls, v: Any) -> Any:
        """Accept namespace values returned as page refs like {'id': N}."""
        if not isinstance(v, dict):
            return v

        namespace = v.get("namespace")
        if isinstance(namespace, dict):
            v = dict(v)
            v["namespace"] = namespace.get("name", "")
        elif isinstance(namespace, int):
            v = dict(v)
            v["namespace"] = ""
        return v

    @property
    def display_name(self) -> str:
        """Human-readable page name, preserving original casing.

        Logseq stores the canonical `name` lowercased as a unique id and keeps the
        user-supplied spelling in `original-name`. Surface the latter so callers
        show and reuse the intended casing instead of echoing the lowercased slug
        (which Logseq would subsequently adopt as the page's display name).
        """
        return self.original_name or self.name


class BlockEntity(BaseModel):
    """A Logseq block, as returned by getPageBlocksTree / getBlock."""

    model_config = ConfigDict(populate_by_name=True)

    id: int = 0
    uuid: str = ""
    content: str = ""
    format: str = "markdown"
    marker: str | None = None
    priority: str | None = None
    page: PageRef | None = None
    parent: BlockRef | None = None
    left: BlockRef | None = None
    children: list["BlockEntity"] = Field(default_factory=list)
    properties: dict = Field(default_factory=dict)
    refs: list = Field(default_factory=list)
    path_refs: list = Field(default_factory=list, alias="path-refs")
    pre_block: bool = Field(False, alias="pre-block?")

    @model_validator(mode="before")
    @classmethod
    def handle_compact_children(cls, v: Any) -> Any:
        """Drop compact children format [["uuid", "value"]] — keep children=[]."""
        if isinstance(v, dict) and "children" in v:
            children = v["children"]
            if children and isinstance(children[0], list):
                v = dict(v)
                v["children"] = []
        return v


# Required after class definition so self-referential type resolves correctly.
BlockEntity.model_rebuild()
