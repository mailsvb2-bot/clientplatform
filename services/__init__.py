"""Shared ClientPlatform services namespace.

Runtime code imports concrete service modules explicitly. Keeping this package
namespace empty prevents subpackages such as ``services.db`` from being shadowed
by convenience callables.
"""

__all__: list[str] = []
