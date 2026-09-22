"""Stable Python extension boundary for new algorithms.

Declarative modules remain data-only. Code extensions are application code:
register trusted handlers at startup; a manifest cannot import or execute code.
"""
from dataclasses import dataclass
from typing import Callable
from .schemas import BusinessError


@dataclass(frozen=True)
class CommandDefinition:
    name: str
    label: str
    handler: Callable
    input_schema: dict
    version: int = 1
    module: str = 'custom'


@dataclass(frozen=True)
class QueryDefinition:
    name: str
    label: str
    handler: Callable
    input_schema: dict
    version: int = 1
    module: str = 'custom'


class ExtensionRegistry:
    def __init__(self):
        self.commands, self.queries = {}, {}

    def register(self, definition):
        import re
        if not re.fullmatch(r'[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*', definition.name):
            raise ValueError('Code capabilities must use module.operation names')
        if definition.version != 1 or not callable(definition.handler):
            raise ValueError('Unsupported capability version or handler')
        target = self.commands if isinstance(definition, CommandDefinition) else self.queries
        if definition.name in target:
            raise ValueError('Capability is already registered')
        def check_schema(value):
            if isinstance(value, dict):
                if '$ref' in value:
                    raise ValueError('Self-contained schemas only; remote references are not allowed')
                for child in value.values():
                    check_schema(child)
            elif isinstance(value, list):
                for child in value:
                    check_schema(child)
        check_schema(definition.input_schema)
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(definition.input_schema)
        target[definition.name] = definition

    @staticmethod
    def validate(definition, payload):
        from jsonschema import Draft202012Validator
        errors = list(Draft202012Validator(definition.input_schema).iter_errors(payload))
        if errors:
            raise BusinessError('extension_validation', '扩展操作参数不符合已注册的格式。', {'path': list(errors[0].absolute_path), 'reason': errors[0].message[:200]})

    def describe(self):
        return [{'name': d.name, 'label': d.label, 'version': d.version, 'module': d.module, 'kind': kind, 'input_schema': d.input_schema} for kind, definitions in [('command', self.commands), ('query', self.queries)] for d in definitions.values()]
