"""Application adapters.

Adapters are deliberately kept outside :mod:`computer_use_linux.session`.  A new adapter is
just a module in this package with a class decorated by :func:`register`; the CLI and MCP
surfaces discover it without another core-code edit.

The interaction order for this package is intentional:

1. use the application's native API when one is available;
2. use AT-SPI ``Action.do_action()`` when the target is an accessible control;
3. use screenshot-derived pixel coordinates only as the final fallback.

On Wayland AT-SPI cannot report a trustworthy global window origin, so adapter code must never
turn AT-SPI extents into click coordinates.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from ..config import Config, load_config
from ..errors import BackendUnavailable
from ..safety import require_confirmation


@dataclass(frozen=True)
class ActionSpec:
    """Machine-readable description of one adapter action.

    ``parameters`` is a small JSON-schema-like mapping.  Each value may contain ``type``,
    ``description``, ``default`` and ``required``.  Keeping this representation independent of
    argparse and MCP lets both frontends expose exactly the same action contract.
    """

    name: str
    description: str
    parameters: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()
    confirmation: bool = False

    @property
    def dangerous(self) -> bool:
        return self.confirmation or self.name.replace("-", "_") in {"eval", "run_python", "eval_gdscript"}

    def parameter(self, name: str) -> Mapping[str, Any] | None:
        if name in self.parameters:
            return self.parameters[name]
        return None


class AppAdapter(Protocol):
    """Protocol implemented by every controllable application adapter."""

    name: str
    needs_session: bool

    def detect(self) -> bool:
        """Return whether a managed, controllable instance is reachable now."""

    def launch(self, **kwargs: Any) -> Any:
        """Launch an instance owned by this adapter."""

    def actions(self) -> list[ActionSpec]:
        """Return the adapter's frontend-neutral action descriptions."""

    def invoke(self, action: str, **kwargs: Any) -> Any:
        """Invoke one action, including its safety checks."""

    def surface_hint(self) -> str | None:
        """Return a best-effort surface id, or ``None`` when Wayland hides it."""


class AdapterBase:
    """Shared safety and contract plumbing; application mechanisms stay in subclasses."""

    name = ""
    needs_session = False

    def __init__(self, *, session: Any | None = None, config: Config | None = None):
        self.session = session
        self.config = config or getattr(session, "config", None) or load_config()

    def action_spec(self, action: str) -> ActionSpec:
        wanted = action.replace("-", "_")
        for spec in self.actions():
            names = {spec.name.replace("-", "_"), *(alias.replace("-", "_") for alias in spec.aliases)}
            if wanted in names:
                return spec
        raise ValueError(f"unknown {self.name} adapter action: {action}")

    def invoke(self, action: str, **kwargs: Any) -> Any:
        spec = self.action_spec(action)
        canonical = spec.name.replace("-", "_")
        confirm = bool(kwargs.pop("confirm", False))
        if spec.dangerous or self.config.confirm_mode == "all":
            confirmation_action = "eval" if canonical in {"eval", "run_python", "eval_gdscript"} else canonical
            require_confirmation(
                action=confirmation_action,
                confirm=confirm,
                confirm_mode=self.config.confirm_mode,
            )
        return self._invoke(canonical, **kwargs)

    def _invoke(self, action: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    def surface_hint(self) -> str | None:
        return None

    def close(self) -> None:
        """Release adapter-local resources without stopping user-visible managed apps."""


_FACTORIES: dict[str, type[AdapterBase]] = {}
_DISCOVERED = False
_IMPORT_ERRORS: dict[str, str] = {}


def register(cls: type[AdapterBase] | None = None, *, name: str | None = None):
    """Register an adapter class, normally used as ``@register``."""

    def decorator(adapter_cls: type[AdapterBase]) -> type[AdapterBase]:
        adapter_name = (name or getattr(adapter_cls, "name", "")).strip().replace("-", "_")
        if not adapter_name:
            raise ValueError("an adapter must define a non-empty name")
        if adapter_name in _FACTORIES and _FACTORIES[adapter_name] is not adapter_cls:
            raise ValueError(f"duplicate adapter name: {adapter_name}")
        _FACTORIES[adapter_name] = adapter_cls
        return adapter_cls

    return decorator(cls) if cls is not None else decorator


def discover() -> None:
    """Import every adapter module in this package exactly once.

    Imports are intentionally isolated.  An optional host dependency can make one module
    unavailable without preventing ``cul app list`` or the other adapters from loading.
    Adapter modules therefore keep optional imports lazy, but this guard makes the registry
    fail-soft even when a third-party integration is installed incorrectly.
    """

    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True
    for module_info in pkgutil.iter_modules(__path__):
        module_name = module_info.name
        if module_name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{module_name}")
        except Exception as exc:  # pragma: no cover - only exercised by broken optional hosts
            _IMPORT_ERRORS[module_name] = f"{type(exc).__name__}: {exc}"


def adapter_factories() -> dict[str, type[AdapterBase]]:
    discover()
    return dict(sorted(_FACTORIES.items()))


def registered_names() -> list[str]:
    return list(adapter_factories())


def create_adapters(*, session: Any | None = None, config: Config | None = None) -> dict[str, AppAdapter]:
    """Instantiate all registered adapters with the shared session when one exists."""

    chosen_config = config or getattr(session, "config", None) or load_config()
    result: dict[str, AppAdapter] = {}
    for name, factory in adapter_factories().items():
        result[name] = factory(session=session, config=chosen_config)
    return result


def get_adapter(name: str, *, session: Any | None = None, config: Config | None = None) -> AppAdapter:
    normalized = name.replace("-", "_")
    adapters = create_adapters(session=session, config=config)
    try:
        return adapters[normalized]
    except KeyError as exc:
        known = ", ".join(adapters) or "none"
        raise BackendUnavailable(f"unknown adapter {name!r}; available adapters: {known}") from exc


def import_errors() -> dict[str, str]:
    discover()
    return dict(_IMPORT_ERRORS)


def action_parameters(
    spec: ActionSpec,
    *,
    include_confirmation: bool = True,
    force_confirmation: bool = False,
) -> dict[str, Mapping[str, Any]]:
    """Return a copy of an action schema, adding the standard safety confirmation field."""

    parameters = {str(name): dict(value) for name, value in spec.parameters.items()}
    if include_confirmation and (spec.dangerous or force_confirmation):
        parameters.setdefault(
            "confirm",
            {
                "type": "boolean",
                "default": False,
                "description": "Confirm this adapter action; required by the safety policy.",
            },
        )
    return parameters


def _annotation(schema: Mapping[str, Any]) -> Any:
    kind = schema.get("type", "string")
    if kind == "boolean":
        return bool
    if kind == "integer":
        return int
    if kind == "number":
        return float
    if kind == "array":
        return list[Any]
    if kind == "object":
        return dict[str, Any]
    return str


def mcp_tool_function(adapter: AppAdapter, spec: ActionSpec, *, action_name: str | None = None) -> Any:
    """Create a callable with a real inspect signature for the MCP schema generator."""

    exposed_action = action_name or spec.name
    force_confirmation = getattr(getattr(adapter, "config", None), "confirm_mode", "destructive") == "all"
    parameters = action_parameters(spec, force_confirmation=force_confirmation)
    required = {
        str(name)
        for name, value in parameters.items()
        if bool(value.get("required", False)) or ("default" not in value and name != "confirm")
    }

    def invoke_tool(**kwargs: Any) -> Any:
        return adapter.invoke(exposed_action, **kwargs)

    invoke_tool.__name__ = f"app_{adapter.name}_{exposed_action.replace('-', '_')}"
    invoke_tool.__doc__ = spec.description
    annotations: dict[str, Any] = {}
    signature_parameters: list[inspect.Parameter] = []
    for name, schema in parameters.items():
        annotation = _annotation(schema)
        annotations[name] = annotation
        default = inspect.Parameter.empty if name in required else schema.get("default", None)
        signature_parameters.append(
            inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation)
        )
    annotations["return"] = Any
    invoke_tool.__annotations__ = annotations
    invoke_tool.__signature__ = inspect.Signature(signature_parameters, return_annotation=Any)
    return invoke_tool


__all__ = [
    "ActionSpec",
    "AdapterBase",
    "AppAdapter",
    "action_parameters",
    "adapter_factories",
    "create_adapters",
    "discover",
    "get_adapter",
    "import_errors",
    "mcp_tool_function",
    "register",
    "registered_names",
]
