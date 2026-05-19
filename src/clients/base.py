"""Abstract base class for Anker Solix F3800 monitor clients."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Callable, Awaitable

from src.models import F3800Data
from src.config import AppSettings

logger = logging.getLogger(__name__)

# Callback type: async function that receives F3800Data
DataCallback = Callable[[F3800Data], Awaitable[None]]


class BaseMonitor(ABC):
    """Abstract base class for F3800 connection monitors.

    Subclasses implement the specific connection logic (MQTT cloud, Modbus local, etc.)
    and call the registered callbacks whenever new data is received.
    """

    def __init__(self, config: AppSettings) -> None:
        self._config = config
        self._callbacks: list[DataCallback] = []
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        """Whether the monitor is currently active."""
        return self._running

    def add_callback(self, callback: DataCallback) -> None:
        """Register a callback to be called when new F3800 data is received."""
        self._callbacks.append(callback)

    def remove_callback(self, callback: DataCallback) -> None:
        """Remove a previously registered callback."""
        self._callbacks.remove(callback)

    async def _notify(self, data: F3800Data) -> None:
        """Call all registered callbacks with new data."""
        for callback in self._callbacks:
            try:
                await callback(data)
            except Exception:
                logger.exception("Error in data callback")

    @abstractmethod
    async def start(self) -> None:
        """Start the monitor connection and data collection."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the monitor and disconnect."""
        ...

    @abstractmethod
    async def get_device_info(self) -> dict:
        """Get static device information (model, SN, firmware, etc.)."""
        ...
