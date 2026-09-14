"""Thin ClientPlatform HTTP adapter for the pinned public UCR gRPC services."""

from .server import UCR_PINNED_REVISION, GatewayConfigurationError, GatewayService, SidecarConfig

__all__ = [
    "UCR_PINNED_REVISION",
    "GatewayConfigurationError",
    "GatewayService",
    "SidecarConfig",
]
