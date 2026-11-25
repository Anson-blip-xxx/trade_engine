"""Pure, immutable strategy definitions and account deployment snapshots.

Handler names are explicitly registered identifiers, never imports or executable
expressions. Resolving a handler does not invoke it, reserve risk, authorize an
order, enforce a deployment transition, or isolate its execution time. Runtime
code must separately persist/fence deployments and enforce execution budgets.

The consumer keeps the existing account-plus-producer shape: ``instance_id``
occupies the producer slot and stays stable across version/configuration changes.
This module neither changes existing consumers nor implements cross-strategy
opportunity deduplication. Retain registered old versions while positions need
their exit handlers, including after entry is disabled.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.state import normalized

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_MODES = frozenset({"SHADOW", "ACTIVE", "DRAINING", "OFF"})
MAX_DEPLOYMENT_CONFIG_BYTES = 65536


def strategy_identifier(value):
    """Validate a bounded, non-executable strategy/handler identifier."""
    normalized(value)
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("STRATEGY_IDENTIFIER_INVALID")
    return value


def deployment_generation(value):
    """Positive generations stay exact in JSON consumers; bool is not an int."""
    if type(value) is not int or not 1 <= value <= 2**53 - 1:
        raise ValueError("STRATEGY_GENERATION_INVALID")
    return value


def _config_json(encoded):
    if type(encoded) is not str:
        raise TypeError("STRATEGY_CONFIG_JSON_REQUIRED")
    try:
        size = len(encoded.encode("utf-8"))
    except UnicodeError:
        raise ValueError("STRATEGY_CONFIG_INVALID_UNICODE") from None
    if size > MAX_DEPLOYMENT_CONFIG_BYTES:
        raise ValueError("STRATEGY_CONFIG_BOUND")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("STRATEGY_CONFIG_DUPLICATE_FIELD")
            result[key] = value
        return result

    try:
        normalized_config = canonical(json.loads(encoded, object_pairs_hook=unique))
    except RecursionError:
        raise ValueError("STRATEGY_CONFIG_NESTING_BOUND") from None
    # Canonical JSON escapes non-ASCII characters, so also bound the stored
    # representation instead of allowing normalization to exceed the limit.
    if len(normalized_config.encode("utf-8")) > MAX_DEPLOYMENT_CONFIG_BYTES:
        raise ValueError("STRATEGY_CONFIG_BOUND")
    return normalized_config


def _whitelist(values):
    if not isinstance(values, (tuple, list, set, frozenset)) or not values:
        raise ValueError("STRATEGY_EXPLICIT_WHITELIST_REQUIRED")
    return frozenset(strategy_identifier(value) for value in values)


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    code_version: str
    plan_schema: str
    entry_handler: str
    exit_handler: str

    def __post_init__(self):
        for value in asdict(self).values():
            strategy_identifier(value)

    @property
    def identity(self):
        return self.strategy_id, self.code_version

    @property
    def spec_digest(self):
        return digest(canonical(asdict(self)))


@dataclass(frozen=True)
class DeploymentBinding:
    """Frozen intent to deploy; even ACTIVE is not submission authorization.

    Construction validates shape only. Use a registry to verify that the exact
    spec remains registered. ``config`` returns a detached mutable copy; only
    the canonical string and its derived digest belong to this snapshot.
    """

    scope: AccountScope
    instance_id: str
    spec: StrategySpec
    config_json: str
    generation: int
    mode: str

    def __post_init__(self):
        if type(self.scope) is not AccountScope or type(self.spec) is not StrategySpec:
            raise TypeError("STRATEGY_TYPED_SCOPE_AND_SPEC_REQUIRED")
        # Revalidate the actual fields and own the snapshot; a restored typed
        # object alone does not prove that AccountScope's invariants still hold.
        object.__setattr__(self, "scope", AccountScope(**asdict(self.scope)))
        object.__setattr__(self, "spec", StrategySpec(**asdict(self.spec)))
        strategy_identifier(self.instance_id)
        deployment_generation(self.generation)
        if type(self.mode) is not str or self.mode not in _MODES:
            raise ValueError("STRATEGY_MODE_INVALID")
        object.__setattr__(self, "config_json", _config_json(self.config_json))

    @property
    def consumer(self):
        return json.dumps(
            [
                self.scope.exchange,
                self.scope.account_id,
                self.scope.environment,
                self.scope.product,
                self.instance_id,
            ],
            separators=(",", ":"),
        )

    @property
    def config(self):
        return json.loads(self.config_json)

    @property
    def config_digest(self):
        return digest(self.config_json)

    @property
    def binding_digest(self):
        return digest(
            canonical(
                {
                    "scope": asdict(self.scope),
                    "instance_id": self.instance_id,
                    "spec": asdict(self.spec),
                    "config_digest": self.config_digest,
                    "generation": self.generation,
                    "mode": self.mode,
                }
            )
        )

    @property
    def execution_authorized(self):
        return False


@dataclass(frozen=True, init=False)
class StrategyRegistry:
    """A copied, closed whitelist; no registration/import side effects.

    Construct a new registry to add versions. The caller is responsible for
    retaining versions still referenced by pending orders or existing positions.
    Entry lookup supports evaluation in SHADOW and ACTIVE; neither grants order
    authority. Exit lookup remains available in every deployment mode.
    """

    specs: Mapping[tuple[str, str], StrategySpec]
    entry_handlers: frozenset[str]
    exit_handlers: frozenset[str]
    plan_schemas: frozenset[str]

    def __init__(self, specs, *, entry_handlers, exit_handlers, plan_schemas):
        if not isinstance(specs, (tuple, list)):
            raise TypeError("STRATEGY_EXPLICIT_SPECS_REQUIRED")
        entries, exits, schemas = (
            _whitelist(values)
            for values in (entry_handlers, exit_handlers, plan_schemas)
        )
        registered = {}
        for spec in specs:
            if type(spec) is not StrategySpec:
                raise TypeError("STRATEGY_TYPED_SPEC_REQUIRED")
            spec = StrategySpec(**asdict(spec))
            if (
                spec.entry_handler not in entries
                or spec.exit_handler not in exits
                or spec.plan_schema not in schemas
            ):
                raise ValueError("STRATEGY_SPEC_NOT_WHITELISTED")
            if spec.identity in registered:
                raise ValueError("STRATEGY_DUPLICATE_VERSION")
            registered[spec.identity] = spec
        object.__setattr__(self, "specs", MappingProxyType(registered))
        object.__setattr__(self, "entry_handlers", entries)
        object.__setattr__(self, "exit_handlers", exits)
        object.__setattr__(self, "plan_schemas", schemas)

    def resolve(self, strategy_id, code_version):
        key = strategy_identifier(strategy_id), strategy_identifier(code_version)
        try:
            return self.specs[key]
        except KeyError:
            raise ValueError("STRATEGY_VERSION_NOT_REGISTERED") from None

    def bind(
        self, *, scope, instance_id, strategy_id, code_version, config, generation, mode
    ):
        return DeploymentBinding(
            scope=scope,
            instance_id=instance_id,
            spec=self.resolve(strategy_id, code_version),
            config_json=canonical(config),
            generation=generation,
            mode=mode,
        )

    def resolve_binding(self, binding):
        if type(binding) is not DeploymentBinding:
            raise TypeError("STRATEGY_TYPED_BINDING_REQUIRED")
        spec = self.resolve(*binding.spec.identity)
        if spec != binding.spec:
            raise ValueError("STRATEGY_SPEC_IDENTITY_CONFLICT")
        return spec

    def resolve_entry(self, binding):
        spec = self.resolve_binding(binding)
        if binding.mode not in {"SHADOW", "ACTIVE"}:
            raise ValueError("STRATEGY_ENTRY_DISABLED")
        return spec

    def resolve_exit(self, binding):
        return self.resolve_binding(binding)
