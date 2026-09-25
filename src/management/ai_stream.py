"""Bounded public progress extracted from an otherwise private JSON stream.

Only a top-level ``summary`` string is exposed. No partial candidate is usable
as a command; callers must still validate the authoritative completed message.
"""
from __future__ import annotations

import re


class StreamLimitError(ValueError):
    """The provider exceeded an in-memory output budget."""


class SummaryPreview:
    """Incremental JSON-prefix recognizer that retains only the summary.

Strings outside the summary are discarded while scanning. Incomplete escapes,
including UTF-16 surrogate pairs, are held until they can be decoded safely.
Invalid prefixes permanently hide the preview; final validation belongs to the
caller. Complexity is linear in input length, with bounded nesting and tokens.
"""

    def __init__(self, *, max_bytes=262_144, max_chars=32_000, max_depth=64):
        self.max_bytes, self.max_chars, self.max_depth = max_bytes, max_chars, max_depth
        self.bytes_seen = 0
        self.invalid = False
        self.finished = False
        self.stack = []
        self.string_role = None
        self.string_value = []
        self.string_count = 0
        self.escape = False
        self.unicode_digits = None
        self.high_surrogate = None
        self.primitive = None
        self.summary = []
        self.summary_seen = False

    @property
    def text(self):
        return '' if self.invalid else ''.join(self.summary)

    def feed(self, fragment):
        if not isinstance(fragment, str):
            raise ValueError('Provider fragment must be text.')
        try:
            self.bytes_seen += len(fragment.encode('utf-8'))
        except UnicodeEncodeError as exc:
            raise ValueError('Provider fragment contains invalid Unicode.') from exc
        if self.bytes_seen > self.max_bytes:
            raise StreamLimitError('Provider preview exceeds output budget.')
        for char in fragment:
            if self.invalid:
                break
            self._accept(char)
        return self.text

    def _fail(self):
        self.invalid = True
        self.summary.clear()
        self.string_value.clear()
        self.stack.clear()
        self.primitive = None

    def _string_char(self, char):
        if 0xD800 <= ord(char) <= 0xDFFF:
            self._fail()
            return
        self.string_count += 1
        if self.string_role == 'summary':
            if self.string_count > self.max_chars:
                self._fail()
                return
            self.summary.append(char)
        elif self.string_role == 'key' and self.string_count <= 64:
            self.string_value.append(char)

    def _unicode_char(self, value):
        if 0xD800 <= value <= 0xDBFF:
            if self.high_surrogate is not None:
                self._fail()
            else:
                self.high_surrogate = value
        elif 0xDC00 <= value <= 0xDFFF:
            if self.high_surrogate is None:
                self._fail()
            else:
                self._string_char(chr(0x10000 + ((self.high_surrogate - 0xD800) << 10) + value - 0xDC00))
                self.high_surrogate = None
        elif self.high_surrogate is not None:
            self._fail()
        else:
            self._string_char(chr(value))

    def _accept_string(self, char):
        if self.unicode_digits is not None:
            if char not in '0123456789abcdefABCDEF':
                self._fail()
                return
            self.unicode_digits += char
            if len(self.unicode_digits) == 4:
                value = int(self.unicode_digits, 16)
                self.unicode_digits = None
                self._unicode_char(value)
            return
        if self.escape:
            self.escape = False
            if char == 'u':
                self.unicode_digits = ''
                return
            escaped = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t'}
            if char not in escaped or self.high_surrogate is not None:
                self._fail()
            else:
                self._string_char(escaped[char])
            return
        if char == '\\':
            self.escape = True
        elif self.high_surrogate is not None or ord(char) < 32:
            self._fail()
        elif char == '"':
            role = self.string_role
            self.string_role = None
            if role == 'key':
                self.stack[-1]['key'] = ''.join(self.string_value) if self.string_count <= 64 else None
                self.stack[-1]['state'] = 'colon'
            else:
                self._value_done()
            self.string_value.clear()
        else:
            self._string_char(char)

    def _start_string(self, role):
        self.string_role, self.string_count = role, 0
        self.string_value.clear()

    def _value_done(self):
        if self.stack:
            self.stack[-1]['state'] = 'comma_or_end'
            self.stack[-1]['key'] = None
        else:
            self.finished = True

    def _container(self, kind):
        if len(self.stack) >= self.max_depth:
            self._fail()
            return
        self.stack.append({'kind': kind, 'state': 'first_key' if kind == 'object' else 'first_value', 'key': None})

    def _end_container(self):
        self.stack.pop()
        self._value_done()

    def _start_value(self, char):
        if char == '{':
            self._container('object')
        elif char == '[':
            self._container('array')
        elif char == '"':
            summary = len(self.stack) == 1 and self.stack[-1]['key'] == 'summary'
            if summary and self.summary_seen:
                self._fail()
                return
            if summary:
                self.summary_seen = True
            self._start_string('summary' if summary else 'discard')
        elif char in '-0123456789tfn':
            self.primitive = char
        else:
            self._fail()

    def _accept(self, char):
        if self.string_role:
            self._accept_string(char)
            return
        if self.primitive is not None:
            if char not in ' \t\r\n,]}':
                self.primitive += char
                if len(self.primitive) > 128:
                    self._fail()
                return
            token, self.primitive = self.primitive, None
            if token not in ('true', 'false', 'null') and re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', token) is None:
                self._fail()
                return
            self._value_done()
        if char in ' \t\r\n':
            return
        if self.finished:
            self._fail()
            return
        if not self.stack:
            if char == '{':
                self._container('object')
            else:
                self._fail()
            return
        frame = self.stack[-1]
        kind, state = frame['kind'], frame['state']
        if state in ('first_key', 'key'):
            if char == '"':
                self._start_string('key')
            elif state == 'first_key' and char == '}':
                self._end_container()
            else:
                self._fail()
        elif state == 'colon':
            if char == ':':
                frame['state'] = 'value'
            else:
                self._fail()
        elif state == 'comma_or_end':
            if char == ',':
                frame['state'] = 'key' if kind == 'object' else 'value'
            elif char == ('}' if kind == 'object' else ']'):
                self._end_container()
            else:
                self._fail()
        elif state == 'first_value' and char == ']':
            self._end_container()
        else:
            self._start_value(char)


def matching_event(body, thread_id, turn_id):
    """Legacy test/protocol events may omit IDs, but an explicit mismatch is ignored."""
    return (body.get('threadId', thread_id) == thread_id and
            body.get('turnId', turn_id) == turn_id)


class ProposalStream:
    """Separate bounded item streams; completed text replaces delta previews."""

    def __init__(self, max_bytes=262_144, max_items=64):
        self.max_bytes, self.max_items = max_bytes, max_items
        self.delta_bytes = self.completed_bytes = 0
        self.parsers = {}
        self.completed = {}
        self.active_item = None

    def _item(self, item_id):
        if not isinstance(item_id, str) or not item_id or len(item_id) > 200:
            raise ValueError('Invalid provider item identifier.')
        if item_id not in self.parsers:
            if len(self.parsers) >= self.max_items:
                raise StreamLimitError('Too many provider message items.')
            self.parsers[item_id] = SummaryPreview(max_bytes=self.max_bytes)
        return self.parsers[item_id]

    @property
    def preview(self):
        parser = self.parsers.get(self.active_item)
        return parser.text if parser else ''

    def start(self, item_id):
        self._item(item_id)
        if item_id not in self.completed:
            self.active_item = item_id
        return self.preview

    def delta(self, item_id, text):
        parser = self._item(item_id)
        if not isinstance(text, str):
            raise ValueError('Invalid provider message delta.')
        self.delta_bytes += len(text.encode('utf-8'))
        if self.delta_bytes > self.max_bytes:
            raise StreamLimitError('Provider deltas exceed output budget.')
        if item_id not in self.completed:
            self.active_item = item_id
            parser.feed(text)
        return self.preview

    def complete(self, item_id, text):
        self._item(item_id)
        if not isinstance(text, str):
            raise ValueError('Invalid completed provider message.')
        self.completed_bytes += len(text.encode('utf-8'))
        if self.completed_bytes > self.max_bytes:
            raise StreamLimitError('Completed provider messages exceed output budget.')
        parser = SummaryPreview(max_bytes=self.max_bytes)
        parser.feed(text)
        self.parsers[item_id] = parser
        self.completed.pop(item_id, None)
        self.completed[item_id] = text
        self.active_item = item_id
        return self.preview
