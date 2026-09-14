"""Repository-owned control plane for benchmark Agent Skills."""

__all__ = ["BenchmarkAgent", "Catalog"]


def __getattr__(name: str):
    if name == "BenchmarkAgent":
        from .application import BenchmarkAgent

        return BenchmarkAgent
    if name == "Catalog":
        from .catalog import Catalog

        return Catalog
    raise AttributeError(name)
