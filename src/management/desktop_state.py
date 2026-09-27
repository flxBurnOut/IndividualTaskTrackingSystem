"""Bounded, in-memory projection of one native desktop conversation stream.

This module consumes the desktop's version-11 snapshots and Immer patches. It
does not start turns, read rollouts, write transcripts, or synthesize missed
events. After loss of continuity the caller must subscribe for a fresh snapshot.
"""
from __future__ import annotations

import copy
import json
import math
from typing import Any, Literal

StreamResult = Literal['accepted', 'ignored', 'resubscribe']
_MAX_REVISION = (1 << 53) - 1
_RESERVED_PATH_KEYS = frozenset({'__proto__', 'constructor', 'prototype'})


class _InvalidState(ValueError):
    pass


class ThreadSnapshot:
    """Track one explicitly bound thread/host/owner; never infer a new owner.

    ``accept`` returns ``accepted`` for an applied snapshot/update, ``ignored``
    for unrelated or stale traffic, and ``resubscribe`` when a new snapshot is
    required. ``revision`` is None while unready. Construct a new instance after
    rediscovering a different owner. Payloads and returned state are never shared
    with the caller, and no transcript is persisted by this class.
    """

    def __init__(self, thread_id: str, host_id: str, owner_client_id: str, *,
                 max_bytes: int = 8 * 1024 * 1024, max_nodes: int = 100_000,
                 max_depth: int = 64, max_patches: int = 4096):
        for value in (thread_id, host_id, owner_client_id):
            if not isinstance(value, str) or not value.strip():
                raise ValueError('Stream identity must be a nonempty string')
        for value in (max_bytes, max_nodes, max_depth, max_patches):
            if type(value) is not int or value <= 0:
                raise ValueError('Stream bounds must be positive integers')
        if max_depth > 128:
            raise ValueError('Stream nesting limit cannot exceed 128')
        self.thread_id = thread_id
        self.host_id = host_id
        self.owner_client_id = owner_client_id
        self.max_bytes = max_bytes
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self.max_patches = max_patches
        self._state: dict[str, Any] | None = None
        self._revision: int | None = None
        # A delayed snapshot older than an already-observed missing update
        # cannot repair that gap, even after the public revision was cleared.
        self._required_revision = 0

    @property
    def revision(self) -> int | None:
        return self._revision

    @property
    def needs_snapshot(self) -> bool:
        return self._state is None

    def get_state(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._state)

    def accept(self, event: Any) -> StreamResult:
        if (type(event) is not dict or event.get('type') != 'broadcast'
                or event.get('method') != 'thread-stream-state-changed'):
            return 'ignored'
        params = event.get('params')
        if (type(params) is not dict or params.get('conversationId') != self.thread_id
                or params.get('hostId') != self.host_id):
            return 'ignored'
        if event.get('sourceClientId') != self.owner_client_id:
            return self._invalidate()
        if type(event.get('version')) is not int or event['version'] != 11:
            return self._invalidate()

        next_revision = None
        try:
            message = self._bounded_copy(event)
            change = message['params'].get('change')
            if type(change) is not dict:
                raise _InvalidState()
            next_revision = change.get('revision')
            if not self._valid_revision(next_revision):
                next_revision = None
                raise _InvalidState()

            if change.get('type') == 'snapshot':
                state = change.get('conversationState')
                self._check_identity(state)
                if next_revision < self._required_revision:
                    return 'ignored'
                if self._revision is not None and next_revision <= self._revision:
                    return 'ignored'
                self._state = state
                self._revision = next_revision
                self._required_revision = next_revision
                return 'accepted'

            if change.get('type') != 'patches':
                raise _InvalidState()
            if self._revision is not None and next_revision <= self._revision:
                return 'ignored'
            if next_revision < self._required_revision:
                return 'ignored'
            base = change.get('baseRevision')
            if (not self._valid_revision(base) or next_revision != base + 1
                    or self._state is None or base != self._revision):
                raise _InvalidState()
            patches = change.get('patches')
            if type(patches) is not list or len(patches) > self.max_patches:
                raise _InvalidState()
            for patch in patches:
                self._check_patch(patch)

            state: Any = copy.deepcopy(self._state)
            # Immer discards the prefix before the last root replacement.
            # Validate all patch shapes above, but do not traverse superseded
            # paths that the actual desktop would never apply.
            start = 0
            for index in range(len(patches) - 1, -1, -1):
                if patches[index]['path'] == []:
                    state = copy.deepcopy(patches[index]['value'])
                    start = index + 1
                    break
            for patch in patches[start:]:
                self._apply_patch(state, patch)
            state = self._bounded_copy(state)
            self._check_identity(state)
            self._state = state
            self._revision = next_revision
            self._required_revision = next_revision
            return 'accepted'
        except (_InvalidState, ValueError, TypeError, KeyError, IndexError,
                OverflowError, RecursionError):
            return self._invalidate(next_revision)

    @staticmethod
    def _valid_revision(value: Any) -> bool:
        return type(value) is int and 0 <= value <= _MAX_REVISION

    def _invalidate(self, required_revision: int | None = None) -> StreamResult:
        if self._revision is not None:
            self._required_revision = max(self._required_revision, self._revision)
        if self._valid_revision(required_revision):
            self._required_revision = max(self._required_revision, required_revision)
        self._state = None
        self._revision = None
        return 'resubscribe'

    def _check_identity(self, state: Any) -> None:
        if type(state) is not dict or state.get('id') != self.thread_id:
            raise _InvalidState()
        if 'hostId' in state and state['hostId'] != self.host_id:
            raise _InvalidState()

    def _bounded_copy(self, value: Any) -> Any:
        """Validate/copy JSON without recursion into unbounded or cyclic input.

        The depth bound is checked before descent. Size uses compact UTF-8 JSON
        byte counts, including keys and punctuation, without serializing the
        entire document into another potentially large intermediate string.
        """
        size = 0
        nodes = 0
        active: set[int] = set()

        def charge(amount: int) -> None:
            nonlocal size
            size += amount
            if size > self.max_bytes:
                raise _InvalidState()

        def string_size(text: str) -> int:
            if len(text) > self.max_bytes:
                raise _InvalidState()
            return len(json.dumps(text, ensure_ascii=False).encode('utf-8'))

        def clone(item: Any, depth: int) -> Any:
            nonlocal nodes
            nodes += 1
            if nodes > self.max_nodes or depth > self.max_depth:
                raise _InvalidState()
            kind = type(item)
            if item is None:
                charge(4)
            elif kind is bool:
                charge(4 if item else 5)
            elif kind is int:
                charge(len(str(item)))
            elif kind is float:
                if not math.isfinite(item):
                    raise _InvalidState()
                charge(len(json.dumps(item)))
            elif kind is str:
                charge(string_size(item))
            elif kind in (dict, list):
                identity = id(item)
                if identity in active:
                    raise _InvalidState()
                active.add(identity)
                charge(2 + max(0, len(item) - 1))
                try:
                    if kind is dict:
                        result = {}
                        for key, child in item.items():
                            if type(key) is not str:
                                raise _InvalidState()
                            charge(string_size(key) + 1)
                            result[key] = clone(child, depth + 1)
                    else:
                        result = [clone(child, depth + 1) for child in item]
                    return result
                finally:
                    active.remove(identity)
            else:
                raise _InvalidState()
            return item

        return clone(value, 0)

    def _check_patch(self, patch: Any) -> None:
        if type(patch) is not dict or patch.get('op') not in {'add', 'remove', 'replace'}:
            raise _InvalidState()
        path = patch.get('path')
        if type(path) is not list or len(path) > self.max_depth:
            raise _InvalidState()
        if not path and patch['op'] != 'replace':
            raise _InvalidState()
        if patch['op'] != 'remove' and 'value' not in patch:
            raise _InvalidState()
        for segment in path:
            if type(segment) not in (str, int):
                raise _InvalidState()
            if type(segment) is str and segment in _RESERVED_PATH_KEYS:
                raise _InvalidState()
            if type(segment) is int and segment < 0:
                raise _InvalidState()

    @staticmethod
    def _dict_key(segment: str | int) -> str:
        # JavaScript object access coerces numeric keys to strings; Immer path
        # arrays retain numeric components for array positions.
        return str(segment) if type(segment) is int else segment

    @staticmethod
    def _list_index(segment: Any, length: int, *, adding: bool = False) -> int:
        if adding and segment == '-':
            return length
        if type(segment) is not int or segment < 0:
            raise _InvalidState()
        if segment > length or (not adding and segment == length):
            raise _InvalidState()
        return segment

    def _apply_patch(self, state: Any, patch: dict[str, Any]) -> None:
        parent = state
        for segment in patch['path'][:-1]:
            if type(parent) is dict:
                parent = parent[self._dict_key(segment)]
            elif type(parent) is list:
                parent = parent[self._list_index(segment, len(parent))]
            else:
                raise _InvalidState()
        last = patch['path'][-1]
        op = patch['op']
        if type(parent) is dict:
            key = self._dict_key(last)
            if op == 'remove':
                # Immer's JavaScript delete is a no-op for an absent leaf.
                parent.pop(key, None)
            else:
                # Both add and replace assign an object property in Immer.
                parent[key] = copy.deepcopy(patch['value'])
        elif type(parent) is list:
            index = self._list_index(last, len(parent), adding=op == 'add')
            if op == 'remove':
                del parent[index]
            elif op == 'add':
                parent.insert(index, copy.deepcopy(patch['value']))
            else:
                parent[index] = copy.deepcopy(patch['value'])
        else:
            raise _InvalidState()
