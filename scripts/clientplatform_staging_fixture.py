from __future__ import annotations

import argparse
import base64
import hashlib
from pathlib import Path


_FIXTURE_SHA256 = "bb380bc775c0708f6a567e24f85ba639e955e5e61ef745820baa010557f51d60"
_FIXTURE_BASE64 = (
    "SUQzBAAAAAAAIlRTU0UAAAAOAAADTGF2ZjYxLjcuMTAwAAAAAAAAAAAAAAD/81jAAAAAAAAAAAAASW5mbwAAAA8A"
    "AAAOAAAGnAArKysrKysrOzs7Ozs7O0xMTExMTExcXFxcXFxcbGxsbGxsbH19fX19fX2NjY2NjY2NnZ2dnZ2dnZ2u"
    "rq6urq6uvr6+vr6+vs7Ozs7Ozs7f39/f39/f7+/v7+/v7/////////8AAAAATGF2YzYxLjE5AAAAAAAAAAAAAAAA"
    "JARAAAAAAAAABpx83GYqAAAAAAAAAAAAAAD/8zjEABBAaog/TxgAC7bduAr1er1er1er1ezqxDDQUDWSsNWEjEzH"
    "ATguB0KBkeRATB8HwffghwffzlHn+Udw/ynv6EZAH31gQ539H+gCAEEjBcGt4/5hWIBgEFNYVAnyzJjIa5vcxhhU"
    "cxz/8zjEHhyxmmQJnaAACKOYbkIdgkOBl7oGyOgZIqBlpIIj5EAMliAaDF0LEgbNA2GEc/4ZZDIoxwoIQt/5DRco"
    "uUmhzhzv/yiRUipkXiaMf/8ipFTIvF4xLpdS/4NCUJA0VcAABLIwZFB/P/7/8zjEChRAdjm/3hAA5Gnam5AXCMAU"
    "CYBA1GBsC6YWA8ppwDDG/6QMYr4uhhzBXmEkJGFwdTAuAFR9ep+rUyKu39zPN/TX9P//Tb63bF3/T2/9H+vfe/3J"
    "yUVjBATGFzHrDKSzAJwIYwFQEVP/8zjEGBcAdhgA1/hkA5Q0AxWaEyMuXERjAvAO0zCLDo2pM/pUzeFgMOQ4DKwv"
    "NOVKp5dyNFjG3dtDL+0W+1fuFbSNjX6Xb9Dvo/6Mu53rYmuAdPf/f/N/GXtBSVBgLGCIamFwzGMZpmdU5GL/8zjE"
    "GxWgdiDK7/iAbrEUZSkGmGCfgjJqdVnBMQZhOhkcFhgUT7aY/cOUlO7Oo1WqJ/7FGPRb/69no3dbHP2UXbYp25EZ"
    "6LbF1/6+62BP1G4HAoeGIkbzMAANchM9YNDI/wR40xYB8MHvAsz/8zjEIxMwdhwA5/qAyJPEwwtwwBPgcDMhAhVF"
    "sUFyixfci//+hPR4q1PX999n93sp9d3/0O+p///8X+VKXKMUoyGTRuMAiAWTATwMAwNIJXMTvYPTKkAxEwMMCSMW"
    "D83HgjKiCMlBMFBpK1r/8zjENRKAciAAz/hkdFbewZ/8d//47/201O////3fmv0K3//+D/r0UkIwEC4QmBwhGFo/"
    "mMCAGcd4mJSOtJk7YfmYKIChGvFkc7vJnY2mWgMHCdGhk7qQ/K6nbEX9UZV+v+RV6GchZU7Rd+//8zjEShRAehwA"
    "7/iAd/Z9n7Pya8AAOqCzNfz/uR9sDorVIgcMKDLwM2IaPzZzVEQpOzsKUxHAcDJyGMX6UwAhxEEFU2wR+Yr5mdf1"
    "37/of+7/9m7/4RjfpT0f7vzFSv//+s7KXxZEyjzRcN7/8zjEWBKYci2U37iAzMA3AXjAcAMswR8J4MZqZuDNtQzo"
    "wRgCFMGRqM7IBMWSxMUQeMEABQWYK/U6ITyb/7f+tvkGf9W6S////0fsrv///OHGhqwiAAxwGjAEOzBgbjD05TJi"
    "qDDKWsIxrgP/8zjEbBMwbhwAz/pkjjA7QRU02hTiz8M0j4FKMaDicbNH7hykz///qZ+m3/X5L////b+W+uoK1/By"
    "JtbXe49Z/JyVq2F3AQAAcAgJAswbG81rRY5IIoxCBwvkYCBsFwOQ0WJIJXG7Z4DAYDL/8zjEfhHQdiAA7/iAdECG"
    "ECBAgg5NNiZMmnsGQ5AgQj3dseTu/EQ5AghHu7YmTvfEY5iEZ7vWsnetEY5BCMe7tj0z4AiIAdD2g8+AIiCOh/A9"
    "8AyII6f4f//gZ/+PAggUIcgBioYmByJEoG//8zjElSHB3lw1XTAB8xORzKJSWi+v+ZiiZ2zJ0UA/4GzZAZxyBiDQ"
    "pEiBEuBni4GRRAAmQBhRNHy6Y+BghYDhAXUgEgAtWySTo/GSDVokBERCwv0WSV/nxzR+NCDEamvV/45o+jAgxGmZ"
    "ESP/8zjEbSN6hmBTnKAAjmr//k8TJ0nSaOFYumJqXjL///8+iieSDoKgIOgr//wFTEFNRTMuMTAwVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/8zjEPgAAA0gBwAAAVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVU="
)


def fixture_bytes() -> bytes:
    data = base64.b64decode(_FIXTURE_BASE64, validate=True)
    if hashlib.sha256(data).hexdigest() != _FIXTURE_SHA256:
        raise RuntimeError("clientplatform_staging_fixture_checksum_mismatch")
    return data


def write_fixture(path: str | Path) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(fixture_bytes())
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    args = parser.parse_args()
    target = write_fixture(args.path)
    print(f"clientplatform staging fixture ready: {target.name} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
