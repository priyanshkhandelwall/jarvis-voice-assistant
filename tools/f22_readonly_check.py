"""F22 read-only check (28 October 2026): can JARVIS's libraries SEE this
laptop's Wi-Fi radio, speakers and screen brightness? It only reads. It never
calls SetStateAsync, RequestAccessAsync (which might ask for consent),
SetMasterVolumeLevelScalar, SetMute or set_brightness.

Run: venv\\Scripts\\python.exe tools\\f22_readonly_check.py
"""

from __future__ import annotations

import asyncio


async def radios() -> None:
    """List the Wi-Fi radios and their state (read only)."""
    from winrt.windows.devices.radios import Radio, RadioKind, RadioState

    found = await Radio.get_radios_async()
    print(f"WinRT Radio.get_radios_async: {found.size} radio(s)")
    for radio in found:
        print(f"  kind={RadioKind(radio.kind).name} name={radio.name!r} state={RadioState(radio.state).name}")
    wifi = [r for r in found if r.kind == RadioKind.WI_FI]
    print("  Wi-Fi radio visible:", bool(wifi))


def volume() -> None:
    """Print the speakers' volume and mute state (read only)."""
    import comtypes
    from pycaw.pycaw import AudioUtilities

    comtypes.CoInitialize()
    try:
        speakers = AudioUtilities.GetSpeakers()
        endpoint = speakers.EndpointVolume
        print(f"pycaw: default speakers {speakers.FriendlyName!r}: volume {round(endpoint.GetMasterVolumeLevelScalar() * 100)}%, muted={bool(endpoint.GetMute())}")
    finally:
        comtypes.CoUninitialize()


def brightness() -> None:
    """Print each monitor and its brightness (read only)."""
    import screen_brightness_control as sbc

    for info in sbc.list_monitors_info():
        print(f"screen-brightness-control: {info.get('name')!r} method={getattr(info.get('method'), '__name__', info.get('method'))}")
    print("  brightness now:", sbc.get_brightness())


if __name__ == "__main__":
    for step in (lambda: asyncio.run(radios()), volume, brightness):
        try:
            step()
        except Exception as exc:  # report, never fix anything
            print(f"  failed: {type(exc).__name__}: {exc}")
