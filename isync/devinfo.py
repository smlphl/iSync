"""Fotoğraflar dışındaki cihaz verileri: donanım bilgisi, pil sağlığı, depolama,
yüklü uygulamalar (boyut + ikon), dosya paylaşımlı uygulamaların belgeleri, duvar kağıdı."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.house_arrest import HouseArrestService
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.springboard import SpringBoardServicesService

from .device import Device

log = logging.getLogger("isync.devinfo")
APPLE_EPOCH = 978307200

COLORS = {"1": "Siyah / Uzay Grisi", "2": "Beyaz / Gümüş"}


async def _safe(coro, default=None):
    try:
        return await coro
    except Exception as e:
        log.debug("devinfo: %r", e)
        return default


async def details(dev: Device) -> dict[str, Any]:
    if dev.lockdown is None:
        await dev.connect()
    ld = dev.lockdown
    v: dict[str, Any] = await _safe(ld.get_value(), {}) or {}
    backup = await _safe(ld.get_value("com.apple.mobile.backup"), {}) or {}
    intl = await _safe(ld.get_value("com.apple.international"), {}) or {}
    disk = await _safe(ld.get_value("com.apple.disk_usage"), {}) or {}
    sync = await _safe(ld.get_value("com.apple.mobile.data_sync"), {}) or {}
    wifi = await _safe(ld.get_value("com.apple.mobile.wireless_lockdown"), {}) or {}

    battery: dict[str, Any] = {}
    try:
        async with DiagnosticsService(ld) as d:
            b = await d.get_battery() or {}
        design = b.get("DesignCapacity") or 0
        nominal = b.get("NominalChargeCapacity") or b.get("AppleRawMaxCapacity") or 0
        lt = (b.get("BatteryData") or {}).get("LifetimeData") or {}
        adapter = b.get("AdapterDetails") or {}
        battery = {
            "level": b.get("CurrentCapacity"),
            "charging": bool(b.get("IsCharging")),
            "external": bool(b.get("ExternalConnected")),
            "cycles": b.get("CycleCount"),
            "design_mah": design,
            "full_mah": nominal,
            "health": round(min(100.0, nominal / design * 100), 1) if design and nominal else None,
            "health_raw": round(nominal / design * 100, 1) if design and nominal else None,
            "temp_c": round(b["Temperature"] / 100, 1) if b.get("Temperature") else None,
            "voltage": round(b["Voltage"] / 1000, 2) if b.get("Voltage") else None,
            "avg_temp_c": lt.get("AverageTemperature"),
            "max_temp_c": round(lt["MaximumTemperature"] / 10, 1) if lt.get("MaximumTemperature") else None,
            "adapter": adapter.get("Description"),
            "adapter_watts": adapter.get("Watts"),
        }
    except Exception as e:
        log.warning("Pil bilgisi alınamadı: %r", e)

    last_cloud = backup.get("LastCloudBackupDate")
    hw = {
        "name": v.get("DeviceName"),
        "model": dev.info.get("model"),
        "product_type": v.get("ProductType"),
        "model_number": f"{v.get('ModelNumber', '')}{v.get('RegionInfo', '')}",
        "hardware_model": v.get("HardwareModel"),
        "color": COLORS.get(str(v.get("DeviceColor")), v.get("DeviceColor")),
        "ios": v.get("ProductVersion"),
        "build": v.get("BuildVersion"),
        "serial": v.get("SerialNumber"),
        "imei": v.get("InternationalMobileEquipmentIdentity"),
        "imei2": v.get("InternationalMobileEquipmentIdentity2"),
        "meid": v.get("MobileEquipmentIdentifier"),
        "udid": v.get("UniqueDeviceID"),
        "wifi_mac": v.get("WiFiAddress"),
        "bt_mac": v.get("BluetoothAddress"),
        "baseband": v.get("BasebandVersion"),
        "cpu": v.get("CPUArchitecture"),
        "activation": v.get("ActivationState"),
        "passcode": v.get("PasswordProtected"),
        "timezone": v.get("TimeZone"),
        "language": intl.get("Language"),
        "locale": intl.get("Locale"),
        "carrier": next((c.get("CFBundleIdentifier") for c in v.get("CarrierBundleInfoArray") or [] if isinstance(c, dict)), None),
    }
    return {
        "hardware": hw,
        "battery": battery,
        "storage": {
            "total": int(v.get("TotalDiskCapacity") or disk.get("TotalDiskCapacity") or 0),
            "data_total": int(disk.get("TotalDataCapacity") or 0),
            "data_free": int(disk.get("AmountDataAvailable") or 0),             # şu an gerçekten boş
            "data_purgeable_free": int(disk.get("TotalDataAvailable") or 0),     # iOS'un boşaltabileceği dahil
            "system": int(disk.get("TotalSystemCapacity") or 0),
        },
        "backup": {
            "icloud": backup.get("CloudBackupEnabled"),
            "last_icloud": (last_cloud + APPLE_EPOCH) if isinstance(last_cloud, (int, float)) else None,
            "encrypted_local": backup.get("WillEncrypt"),
        },
        "sync_sources": {k: (val.get("Sources") or []) for k, val in sync.items() if isinstance(val, dict)},
        "wifi_sync": wifi.get("SupportsWifiSyncing"),
    }


async def apps(dev: Device, sizes: bool = True) -> list[dict[str, Any]]:
    """sizes=False anında döner; boyut hesabı iOS tarafında ~1 dk sürebilir."""
    if dev.lockdown is None:
        await dev.connect()
    async with InstallationProxyService(dev.lockdown) as ip:
        raw = await ip.get_apps(application_type="User", calculate_sizes=sizes)
    out = []
    for bid, a in raw.items():
        static = int(a.get("StaticDiskUsage") or 0)
        dynamic = int(a.get("DynamicDiskUsage") or 0)
        out.append({
            "id": bid,
            "name": a.get("CFBundleDisplayName") or a.get("CFBundleName") or bid,
            "version": a.get("CFBundleShortVersionString") or a.get("CFBundleVersion"),
            "app_size": static,
            "data_size": dynamic,
            "size": static + dynamic,
            "sized": sizes,
            "sharing": bool(a.get("UIFileSharingEnabled")),
            "developer": (a.get("SignerIdentity") or "").replace("Apple iPhone OS Application Signing", "App Store"),
        })
    out.sort(key=lambda x: x["size"], reverse=True)
    return out


async def icon(dev: Device, bundle_id: str) -> Optional[bytes]:
    if dev.lockdown is None:
        await dev.connect()
    async with SpringBoardServicesService(dev.lockdown) as sb:
        return await sb.get_icon_pngdata(bundle_id)


async def wallpaper(dev: Device) -> Optional[bytes]:
    if dev.lockdown is None:
        await dev.connect()
    async with SpringBoardServicesService(dev.lockdown) as sb:
        return await sb.get_wallpaper_pngdata()


def _clean(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p and p not in (".", "..")]
    return "/Documents/" + "/".join(parts) if parts else "/Documents"


async def app_docs_list(dev: Device, bundle_id: str, path: str = "") -> list[dict[str, Any]]:
    if dev.lockdown is None:
        await dev.connect()
    base = _clean(path)
    ha = await HouseArrestService.create(dev.lockdown, bundle_id=bundle_id, documents_only=True)
    async with ha:
        names = [n for n in await ha.listdir(base) if n not in (".", "..")]
        out = []
        for n in sorted(names, key=str.lower):
            try:
                st = await ha.stat(f"{base}/{n}")
            except Exception:
                continue
            mt: Optional[datetime] = st.get("st_mtime")
            out.append({
                "name": n,
                "dir": st.get("st_ifmt") == "S_IFDIR",
                "size": int(st.get("st_size", 0)),
                "mtime": mt.timestamp() if mt else None,
            })
        out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
        return out


async def app_docs_read(dev: Device, bundle_id: str, path: str) -> bytes:
    if dev.lockdown is None:
        await dev.connect()
    ha = await HouseArrestService.create(dev.lockdown, bundle_id=bundle_id, documents_only=True)
    async with ha:
        return await ha.get_file_contents(_clean(path))
