from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from riolu.source import Source, matches_source_token, normalize_source_token


@dataclass(frozen=True)
class SourceRegistry:
    sources: tuple[Source, ...]

    @classmethod
    def load(
        cls,
        directory: str,
        enabled_ids: tuple[str, ...] = (),
    ) -> SourceRegistry:
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            if not enabled_ids:
                return cls(())
            raise RuntimeError(f"Source directory does not exist: {root}")

        requested = tuple(dict.fromkeys(normalize_source_token(value) for value in enabled_ids))
        discovered: dict[str, Source] = {}
        load_errors: list[RuntimeError] = []
        for entry in sorted(root.iterdir(), key=lambda value: value.name.casefold()):
            module_path: Path | None = None
            package_dir: Path | None = None
            if entry.is_dir() and (entry / "__init__.py").is_file():
                module_path = entry / "__init__.py"
                package_dir = entry
            elif entry.is_file() and entry.suffix == ".py" and not entry.name.startswith("_"):
                module_path = entry
            if module_path is None:
                continue

            try:
                module = _load_module(module_path, package_dir)
                source = getattr(module, "SOURCE", None)
                _validate_source(source, module_path)
            except RuntimeError as exc:
                if not requested:
                    raise
                load_errors.append(exc)
                continue
            source_id = normalize_source_token(source.id)
            if requested and source_id not in requested:
                continue
            if source_id in discovered:
                raise RuntimeError(f"Duplicate source id: {source.id}")
            discovered[source_id] = source

        if not discovered and enabled_ids:
            raise RuntimeError(f"No source plugins found in {root}") from (load_errors[0] if load_errors else None)
        if not discovered:
            return cls(())

        missing = [value for value in requested if value not in discovered]
        if missing:
            raise RuntimeError(f"Configured source plugins were not found: {', '.join(missing)}") from (load_errors[0] if load_errors else None)
        ordered = requested or tuple(discovered)
        return cls(tuple(discovered[source_id] for source_id in ordered))

    def all(self) -> tuple[Source, ...]:
        return self.sources

    def news_sources(self) -> tuple[Source, ...]:
        return tuple(
            source
            for source in self.sources
            if str(source.category).casefold() != "ctf"
        )

    def automated_sources(self, configured_ids: tuple[str, ...]) -> tuple[Source, ...]:
        enabled = {normalize_source_token(value) for value in configured_ids}
        missing = enabled - {normalize_source_token(source.id) for source in self.sources}
        if missing:
            raise RuntimeError(f"Configured automated sources were not found: {', '.join(sorted(missing))}")
        return tuple(
            source
            for source in self.sources
            if normalize_source_token(source.id) in enabled
        )

    def default_sources(self, configured_ids: tuple[str, ...]) -> tuple[Source, ...]:
        if not configured_ids:
            return self.sources
        selected = {}
        for source_id in configured_ids:
            source = self.get(source_id)
            if source is None:
                raise RuntimeError(f"Configured default source was not found: {source_id}")
            selected[source.id] = source
        return tuple(selected.values())

    def get(self, token: str) -> Source | None:
        for source in self.sources:
            if matches_source_token(source, token):
                return source
        return None

    def command_aliases(self) -> dict[str, Source]:
        aliases: dict[str, Source] = {}
        for source in self.sources:
            aliases[normalize_source_token(source.id)] = source
            for alias in source.aliases:
                aliases[normalize_source_token(alias)] = source
        return aliases


def _load_module(path: Path, package_dir: Path | None) -> ModuleType:
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
    name = f"riolu_local_source_{digest}"
    kwargs = (
        {"submodule_search_locations": [str(package_dir)]}
        if package_dir is not None
        else {}
    )
    spec = importlib.util.spec_from_file_location(name, path, **kwargs)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load source plugin: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise RuntimeError(f"Could not load source plugin {path}: {exc}") from exc
    return module


def _validate_source(source: object, path: Path) -> None:
    fields = ("id", "name", "category", "description", "aliases")
    if source is None or any(not hasattr(source, field) for field in fields):
        raise RuntimeError(f"Source plugin must export a valid SOURCE object: {path}")
    if not callable(getattr(source, "fetch", None)):
        raise RuntimeError(f"Source plugin SOURCE must define async fetch(): {path}")
