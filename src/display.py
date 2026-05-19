"""Real-time console display for Anker Solix F3800 telemetry.

Uses the Rich library for beautiful terminal output with live-updating panels.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.models import F3800Data

logger = logging.getLogger(__name__)


# Map UTC offsets to IANA timezone names for DST-aware display
_OFFSET_TO_IANA: dict[int, str] = {
    -8: "America/Los_Angeles",  # PST / PDT (DST-aware)
    -7: "America/Los_Angeles",  # same zone — DST handles offset
    -6: "America/Chicago",      # CST / CDT
    -5: "America/New_York",      # EST / EDT
     0: "UTC",
    +1: "Europe/Paris",
    +8: "Asia/Shanghai",
    +9: "Asia/Tokyo",
}


def _offset_to_tz(offset: int) -> timezone | ZoneInfo:
    """Convert a UTC offset to the best available timezone.

    Tries IANA zone names first (handles DST automatically),
    falls back to a fixed-offset timezone if zoneinfo data is unavailable.
    """
    iana_name = _OFFSET_TO_IANA.get(offset)
    if iana_name:
        try:
            return ZoneInfo(iana_name)
        except Exception:
            pass  # Fall through to fixed offset
    return timezone(timedelta(hours=offset))


class ConsoleDisplay:
    """Live-updating console display for F3800 telemetry data."""

    def __init__(self, tz_offset: int = -7, temp_unit: str = "F") -> None:
        self._latest: F3800Data | None = None
        self._update_count: int = 0
        self._rich_available: bool = False
        self._console: Any = None
        self._live: Any = None  # rich.live.Live instance
        # Map tz_offset to a proper IANA timezone for DST-aware display
        self._tz: timezone | ZoneInfo = _offset_to_tz(tz_offset)
        # Temperature unit: "F" or "C"
        self._temp_unit: str = temp_unit.upper()
        # Session peak PV tracking (highest values seen during this run)
        self._max_pv_total_w: int = 0
        self._max_pv1_w: int = 0
        self._max_pv2_w: int = 0

        try:
            from rich.console import Console
            from rich.live import Live
            from rich.panel import Panel
            from rich.table import Table
            from rich.layout import Layout
            self._rich_available = True
            self._console = Console()
        except ImportError:
            logger.warning(
                "Rich library not installed. Falling back to plain text output. "
                "Install with: pip install rich"
            )

    def set_temp_unit(self, unit: str) -> None:
        """Change the temperature display unit at runtime."""
        self._temp_unit = unit.upper()

    def start(self) -> None:
        """Start the live display (call before first update)."""
        if self._rich_available:
            from rich.live import Live
            from rich.panel import Panel
            from rich.table import Table
            # Start with a placeholder — Live will update in-place
            placeholder = Panel("Waiting for data...", title="Anker Solix F3800", border_style="blue")
            self._live = Live(placeholder, console=self._console, refresh_per_second=1)
            self._live.start()

    def stop(self) -> None:
        """Stop the live display (call on shutdown)."""
        if self._live is not None:
            self._live.stop()
            self._live = None

    def pause_live(self) -> None:
        """Pause the live display so input() prompt is visible."""
        if self._live is not None:
            self._live.stop()

    def resume_live(self) -> None:
        """Resume the live display after input() is done."""
        if self._live is not None:
            self._live.start()

    async def update(self, data: F3800Data) -> None:
        """Update the display with new F3800 data."""
        self._latest = data
        self._update_count += 1

        # Update session peak PV values
        pv_total = data.photovoltaic_power or 0
        pv1 = data.pv_1_power or 0
        pv2 = data.pv_2_power or 0
        if pv_total > self._max_pv_total_w:
            self._max_pv_total_w = pv_total
        if pv1 > self._max_pv1_w:
            self._max_pv1_w = pv1
        if pv2 > self._max_pv2_w:
            self._max_pv2_w = pv2

        if self._rich_available:
            self._render_rich(data)
        else:
            self._render_plain(data)

    def _render_rich(self, data: F3800Data) -> None:
        """Render using Rich Live for in-place panel updates."""
        from rich.table import Table
        from rich.panel import Panel

        # Build the power table
        table = Table(show_header=True, header_style="bold cyan", expand=False, show_lines=False, padding=(0, 1))
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")
        table.add_column("Unit")

        # Battery
        soc = data.main_battery_soc
        soc_style = "green" if (soc and soc > 50) else "yellow" if (soc and soc > 20) else "red"
        table.add_row("🔋 Battery SOC", f"[{soc_style}]{soc or '—'}[/{soc_style}]", "%")

        # Charging status
        charging = data.is_charging
        if charging is True:
            table.add_row("⚡ Charging", "[green]Yes[/green]", "")
        elif charging is False:
            table.add_row("⚡ Charging", "[dim]No[/dim]", "")
        else:
            table.add_row("⚡ Charging", "—", "")

        # Input power
        ac_in = data.ac_input_power
        solar = data.photovoltaic_power
        total_in = data.total_input_power

        if ac_in is not None:
            style = "green" if ac_in > 0 else "dim"
            table.add_row("🔌 AC Input", f"[{style}]{ac_in}[/{style}]", "W")
        else:
            table.add_row("🔌 AC Input", "—", "W")

        if solar is not None:
            style = "green" if solar > 0 else "dim"
            peak_tag = f" [dim](peak {self._max_pv_total_w}W)[/dim]" if self._max_pv_total_w > solar else ""
            table.add_row("☀️  Solar Input", f"[{style}]{solar}[/{style}]{peak_tag}", "W")
        else:
            table.add_row("☀️  Solar Input", "—", "W")

        if data.pv_1_power is not None or data.pv_2_power is not None:
            pv1_val = data.pv_1_power if data.pv_1_power is not None else '—'
            pv2_val = data.pv_2_power if data.pv_2_power is not None else '—'
            pv1_int = data.pv_1_power or 0
            pv2_int = data.pv_2_power or 0
            pv1_peak_tag = f" [dim](peak {self._max_pv1_w}W)[/dim]" if self._max_pv1_w > pv1_int else ""
            pv2_peak_tag = f" [dim](peak {self._max_pv2_w}W)[/dim]" if self._max_pv2_w > pv2_int else ""
            table.add_row("  PV1 (Patio)", f"{pv1_val}{pv1_peak_tag}", "W")
            table.add_row("  PV2 (Shed)", f"{pv2_val}{pv2_peak_tag}", "W")

        if total_in is not None:
            style = "bold green" if total_in > 0 else "dim"
            table.add_row("📈 Total Input", f"[{style}]{total_in}[/{style}]", "W")
        else:
            table.add_row("📈 Total Input", "—", "W")

        # Output power
        if data.output_power_total is not None:
            style = "yellow" if data.output_power_total > 0 else "dim"
            table.add_row("📉 Total Output", f"[{style}]{data.output_power_total}[/{style}]", "W")
        else:
            table.add_row("📉 Total Output", "—", "W")

        if data.ac_output_power is not None:
            style = "yellow" if data.ac_output_power > 0 else "dim"
            table.add_row("🔌 AC Output", f"[{style}]{data.ac_output_power}[/{style}]", "W")
        else:
            table.add_row("🔌 AC Output", "—", "W")

        # Temperature
        if data.temperature is not None:
            temp_style = "green" if data.temperature < 40 else "yellow" if data.temperature < 55 else "red"
            if self._temp_unit == "F":
                temp_val = data.temperature * 9 / 5 + 32
                table.add_row("🌡️  Temperature", f"[{temp_style}]{temp_val:.0f}[/{temp_style}]", "°F")
            else:
                table.add_row("🌡️  Temperature", f"[{temp_style}]{data.temperature:.0f}[/{temp_style}]", "°C")
        else:
            table.add_row("🌡️  Temperature", "—", f"°{'F' if self._temp_unit == 'F' else 'C'}")

        # Battery charge/discharge
        if data.bat_charge_power is not None:
            style = "green" if data.bat_charge_power > 0 else "dim"
            table.add_row("🔋 Bat Charge", f"[{style}]{data.bat_charge_power}[/{style}]", "W")
        if data.bat_discharge_power is not None:
            style = "yellow" if data.bat_discharge_power > 0 else "dim"
            table.add_row("🔋 Bat Discharge", f"[{style}]{data.bat_discharge_power}[/{style}]", "W")

        # USB ports (show if any have power)
        usb_total = sum(
            p for p in [data.usbc_1_power, data.usbc_2_power, data.usbc_3_power, data.usba_1_power, data.usba_2_power]
            if p is not None
        )
        if usb_total > 0 or any(
            p is not None for p in [data.usbc_1_power, data.usbc_2_power, data.usbc_3_power, data.usba_1_power, data.usba_2_power]
        ):
            table.add_row("🔌 USB-C 1", f"{data.usbc_1_power or 0}", "W")
            table.add_row("🔌 USB-C 2", f"{data.usbc_2_power or 0}", "W")
            table.add_row("🔌 USB-C 3", f"{data.usbc_3_power or 0}", "W")
            table.add_row("🔌 USB-A 1", f"{data.usba_1_power or 0}", "W")
            table.add_row("🔌 USB-A 2", f"{data.usba_2_power or 0}", "W")

        # Expansion packs
        if data.expansion_packs is not None:
            table.add_row("🔋 Expansion Packs", f"{data.expansion_packs}", "")
        for i, exp in enumerate(data.expansions, 1):
            table.add_row(f"  Pack {i} SoC", f"{exp.soc or '—'}", "%")

        # Switches
        ac_sw = data.ac_output_power_switch
        if ac_sw is not None:
            sw_text = "[green]ON[/green]" if ac_sw else "[red]OFF[/red]"
            table.add_row("🔌 AC Output Switch", sw_text, "")

        # Header info — show local time (convert UTC → local using tz_offset)
        if data.timestamp:
            local_ts = data.timestamp.astimezone(self._tz)
            ts = local_ts.strftime("%H:%M:%S %Z")
        else:
            ts = "—"
        source_badge = f"[blue]MQTT[/blue]" if data.source == "mqtt" else f"[magenta]Modbus[/magenta]"
        title = f"Anker Solix F3800  ·  {source_badge}  ·  Update #{self._update_count}  ·  {ts}"

        panel = Panel(table, title=title, border_style="blue", expand=False)

        if self._live is not None:
            # Always call update() — even when paused (is_started=False),
            # Live stores the renderable and displays it on next start/resume.
            # This prevents data updates from being silently dropped while
            # the interactive menu has paused Live for input().
            self._live.update(panel)
        else:
            # Live not started yet — just print once
            self._console.print(panel)

    def _render_plain(self, data: F3800Data) -> None:
        """Render using plain text output (fallback without Rich)."""
        if data.timestamp:
            local_ts = data.timestamp.astimezone(self._tz)
            ts = local_ts.strftime("%H:%M:%S %Z")
        else:
            ts = "—"
        source = data.source.upper()

        print(f"\n{'='*60}")
        print(f"  Anker Solix F3800  |  {source}  |  {ts}  |  #{self._update_count}")
        print(f"{'='*60}")
        print(f"  Battery SOC:    {data.main_battery_soc or '—'}%")
        print(f"  Charging:       {'Yes' if data.is_charging else 'No' if data.is_charging is False else '—'}")
        print(f"  AC Input:       {data.ac_input_power or '—'} W")
        solar = data.photovoltaic_power
        solar_disp = solar if solar is not None else '—'
        solar_peak = f"  (peak {self._max_pv_total_w}W)" if solar is not None and self._max_pv_total_w > solar else ""
        print(f"  Solar Input:   {solar_disp} W{solar_peak}")
        pv1 = data.pv_1_power
        pv2 = data.pv_2_power
        pv1_disp = pv1 if pv1 is not None else '—'
        pv2_disp = pv2 if pv2 is not None else '—'
        pv1_peak = f"  (peak {self._max_pv1_w}W)" if pv1 is not None and self._max_pv1_w > pv1 else ""
        pv2_peak = f"  (peak {self._max_pv2_w}W)" if pv2 is not None and self._max_pv2_w > pv2 else ""
        print(f"  PV1 (Patio):  {pv1_disp} W{pv1_peak}")
        print(f"  PV2 (Shed):   {pv2_disp} W{pv2_peak}")
        total_in = data.total_input_power
        total_disp = total_in if total_in is not None else '—'
        print(f"  Total Input:    {total_disp} W")
        print(f"  Total Output:   {data.output_power_total or '—'} W")
        print(f"  AC Output:      {data.ac_output_power or '—'} W")
        if data.temperature is not None:
            if self._temp_unit == "F":
                temp_val = data.temperature * 9 / 5 + 32
                print(f"  Temperature:    {temp_val:.0f}°F")
            else:
                print(f"  Temperature:    {data.temperature:.0f}°C")
        else:
            print(f"  Temperature:    —")
        if data.ac_output_power_switch is not None:
            print(f"  AC Output Sw:   {'ON' if data.ac_output_power_switch else 'OFF'}")
        print(f"{'='*60}")
