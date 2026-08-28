from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

Document = dict[str, Any]


@dataclass(frozen=True)
class FakeUpdateResult:
    matched_count: int
    modified_count: int
    upserted_id: object | None = None


@dataclass(frozen=True)
class FakeInsertOneResult:
    inserted_id: object


def _values_at(value: object, parts: list[str]) -> list[object]:
    if not parts:
        return [value]
    part = parts[0]
    remaining = parts[1:]
    if isinstance(value, list):
        if part.isdecimal():
            index = int(part)
            if index >= len(value):
                return []
            return _values_at(value[index], remaining)
        values: list[object] = []
        for item in value:
            values.extend(_values_at(item, parts))
        return values
    if not isinstance(value, dict) or part not in value:
        return []
    return _values_at(value[part], remaining)


def _matches(document: Document, query: Document) -> bool:
    for key, expected in query.items():
        values = _values_at(document, key.split("."))
        if isinstance(expected, dict):
            if set(expected) == {"$lt"}:
                if not any(value < expected["$lt"] for value in values):
                    return False
            elif set(expected) == {"$ne"}:
                if any(value == expected["$ne"] for value in values):
                    return False
            else:
                raise AssertionError(f"unsupported fake query operator: {expected!r}")
        elif not any(value == expected for value in values):
            return False
    return True


def _set_path(document: Document, dotted_key: str, value: object) -> None:
    parts = dotted_key.split(".")
    target: object = document
    for part in parts[:-1]:
        if isinstance(target, list):
            target = target[int(part)]
        else:
            assert isinstance(target, dict)
            target = target.setdefault(part, {})
    final = parts[-1]
    if isinstance(target, list):
        target[int(final)] = deepcopy(value)
    else:
        assert isinstance(target, dict)
        target[final] = deepcopy(value)


def _apply_update(document: Document, update: Document, *, inserting: bool) -> Document:
    resolved = deepcopy(document)
    allowed = {"$setOnInsert", "$set", "$inc", "$push"}
    if not set(update).issubset(allowed):
        raise AssertionError(f"unsupported fake update: {update!r}")
    if inserting:
        for key, value in update.get("$setOnInsert", {}).items():
            _set_path(resolved, key, value)
    for key, value in update.get("$set", {}).items():
        _set_path(resolved, key, value)
    for key, value in update.get("$inc", {}).items():
        current = _values_at(resolved, key.split("."))
        if len(current) != 1:
            raise AssertionError("fake increment requires one existing value")
        _set_path(resolved, key, current[0] + value)
    for key, value in update.get("$push", {}).items():
        current = _values_at(resolved, key.split("."))
        if len(current) != 1 or not isinstance(current[0], list):
            raise AssertionError("fake push requires one existing list")
        current[0].append(deepcopy(value))
    return resolved


class FakeCursor:
    def __init__(self, documents: list[Document]) -> None:
        self._documents = deepcopy(documents)

    def sort(self, keys: list[tuple[str, int]]) -> FakeCursor:
        for key, direction in reversed(keys):
            reverse = direction < 0

            def sort_value(document: Document, selected_key: str = key) -> Any:
                return _values_at(document, selected_key.split("."))[0]

            self._documents.sort(
                key=sort_value,
                reverse=reverse,
            )
        return self

    def limit(self, limit: int) -> FakeCursor:
        self._documents = self._documents[:limit]
        return self

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(deepcopy(self._documents))


class FakeCollection:
    def __init__(self, name: str) -> None:
        self.name = name
        self._documents: list[Document] = []
        self._indexes: dict[str, tuple[tuple[tuple[str, int], ...], bool]] = {}
        self._failures: dict[str, OperationFailure] = {}
        self._next_id = 1
        self._lock = RLock()
        self.write_count = 0
        self.call_count: dict[str, int] = {}

    @property
    def documents(self) -> tuple[Document, ...]:
        with self._lock:
            return tuple(deepcopy(self._documents))

    @property
    def indexes(self) -> dict[str, tuple[tuple[tuple[str, int], ...], bool]]:
        with self._lock:
            return deepcopy(self._indexes)

    def fail_next(self, method: str, message: str) -> None:
        self._failures[method] = OperationFailure(message)

    def _called(self, method: str) -> None:
        self.call_count[method] = self.call_count.get(method, 0) + 1
        failure = self._failures.pop(method, None)
        if failure is not None:
            raise failure

    def _identity(self, document: Document) -> object:
        identifier = document.get("_id")
        if identifier is None:
            identifier = self._next_id
            self._next_id += 1
            document["_id"] = identifier
        return identifier

    def _assert_unique(self, candidate: Document, *, replacing_id: object | None = None) -> None:
        for keys, unique in self._indexes.values():
            if not unique:
                continue
            candidate_value = tuple(
                (_values_at(candidate, key.split(".")) or [None])[0] for key, _ in keys
            )
            for existing in self._documents:
                if replacing_id is not None and existing.get("_id") == replacing_id:
                    continue
                existing_value = tuple(
                    (_values_at(existing, key.split(".")) or [None])[0] for key, _ in keys
                )
                if existing_value == candidate_value:
                    raise DuplicateKeyError("unsafe duplicate-key detail")

    def create_index(
        self,
        keys: list[tuple[str, int]],
        *,
        name: str,
        unique: bool = False,
    ) -> str:
        with self._lock:
            self._called("create_index")
            definition = (tuple(keys), unique)
            existing = self._indexes.get(name)
            if existing is not None and existing != definition:
                raise OperationFailure("unsafe incompatible-index detail")
            if existing is None:
                if unique:
                    for document in self._documents:
                        self._assert_unique(document, replacing_id=document.get("_id"))
                self._indexes[name] = definition
                self.write_count += 1
            return name

    def find_one(self, query: Document) -> Document | None:
        with self._lock:
            self._called("find_one")
            for document in self._documents:
                if _matches(document, query):
                    return deepcopy(document)
            return None

    def find(self, query: Document) -> FakeCursor:
        with self._lock:
            self._called("find")
            matching = [
                document for document in self._documents if _matches(document, query)
            ]
            return FakeCursor(matching)

    def insert_one(self, document: Document) -> FakeInsertOneResult:
        with self._lock:
            self._called("insert_one")
            candidate = deepcopy(document)
            identifier = self._identity(candidate)
            self._assert_unique(candidate)
            self._documents.append(candidate)
            self.write_count += 1
            return FakeInsertOneResult(identifier)

    def update_one(
        self,
        query: Document,
        update: Document,
        *,
        upsert: bool = False,
    ) -> FakeUpdateResult:
        with self._lock:
            self._called("update_one")
            for index, document in enumerate(self._documents):
                if _matches(document, query):
                    candidate = _apply_update(document, update, inserting=False)
                    self._assert_unique(candidate, replacing_id=document.get("_id"))
                    modified = candidate != document
                    if modified:
                        self._documents[index] = candidate
                        self.write_count += 1
                    return FakeUpdateResult(1, int(modified))
            if not upsert:
                return FakeUpdateResult(0, 0)
            seed = {
                key: deepcopy(value)
                for key, value in query.items()
                if "." not in key and not isinstance(value, dict)
            }
            candidate = _apply_update(seed, update, inserting=True)
            identifier = self._identity(candidate)
            self._assert_unique(candidate)
            self._documents.append(candidate)
            self.write_count += 1
            return FakeUpdateResult(0, 0, identifier)

    def find_one_and_update(
        self,
        query: Document,
        update: Document,
        *,
        return_document: bool,
    ) -> Document | None:
        with self._lock:
            self._called("find_one_and_update")
            for index, document in enumerate(self._documents):
                if _matches(document, query):
                    before = deepcopy(document)
                    candidate = _apply_update(document, update, inserting=False)
                    self._assert_unique(candidate, replacing_id=document.get("_id"))
                    self._documents[index] = candidate
                    self.write_count += 1
                    returned = candidate if return_document == ReturnDocument.AFTER else before
                    return deepcopy(returned)
            return None


class FakeDatabase:
    def __init__(self) -> None:
        self._collections: dict[str, FakeCollection] = {}
        self.get_collection_calls: list[str] = []

    def get_collection(self, name: str) -> FakeCollection:
        self.get_collection_calls.append(name)
        return self._collections.setdefault(name, FakeCollection(name))

    def collection(self, name: str) -> FakeCollection:
        return self._collections[name]

    @property
    def write_count(self) -> int:
        return sum(collection.write_count for collection in self._collections.values())
