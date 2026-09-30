# ClientPlatform real-device mobile and tablet live E2E

## Scope

This contour extends the Windows live E2E work to real user devices. Each target
is an independent evidence leg; a pass on one device class never substitutes
for another.

Required targets:

- Android phone;
- Android tablet;
- iPhone / iOS;
- iPad / iPadOS;
- HarmonyOS phone;
- HarmonyOS tablet;
- Amazon Fire OS tablet;
- ChromeOS tablet/convertible running Android apps.

Every target exercises Telegram, VK, MAX and the ClientPlatform cockpit. If a
native provider client cannot be installed on a required target, that target is
**BLOCKED-LIVE**. Do not silently replace a native-app claim with a web session.

## Automation stacks

Android phones/tablets, Fire OS and ChromeOS use an Appium 2/3 server with the
UiAutomator2 driver. iPhone and iPad use Appium XCUITest on a macOS host.
HarmonyOS uses a runner-local DevEco Testing Hypium executor through the strict
repository adapter.

The repository never stores messenger login sessions. Dedicated test accounts
remain on the physical devices.

## GitHub runner labels

All device runners have:

`self-hosted, clientplatform-live-e2e`

plus their host OS label and exactly one target label:

- `clientplatform-android-phone`
- `clientplatform-android-tablet`
- `clientplatform-ios-iphone`
- `clientplatform-ipados-ipad`
- `clientplatform-harmonyos-phone`
- `clientplatform-harmonyos-tablet`
- `clientplatform-fireos-tablet`
- `clientplatform-chromeos-tablet`

The live workflow has no pull-request trigger and checks out exact trusted
`main`. Enable it only after all required hardware is provisioned by setting
the repository-level variable `CLIENTPLATFORM_MOBILE_LIVE_E2E_ENABLED=1`.

## Real-device attestation

Set `CLIENTPLATFORM_E2E_DEVICE_UDID` on each dedicated runner.

Android-family runners are verified through ADB. The validator rejects emulator
serials and QEMU, reads physical display size/density, and derives phone versus
tablet at the 600dp boundary. Amazon manufacturer identity selects Fire OS.
The `org.chromium.arc` feature selects ChromeOS.

Apple runners are verified through `xcrun xctrace list devices`. Only devices
listed before the Simulator section are accepted, and iPhone/iPad identity must
match the target.

HarmonyOS runners are verified through HDC connectivity and a runner-local
physical-device/form-factor attestation. They also require a concrete DevEco
Testing Hypium executor path. Until that runner exists, HarmonyOS remains
BLOCKED-LIVE rather than receiving a false PASS.

The validator writes a non-secret identity JSON containing only target metadata
and a SHA-256-derived device reference, not the raw device serial.

## Mobile surface profiles

Each runner provides four non-secret JSON profiles:

- `CLIENTPLATFORM_E2E_TELEGRAM_MOBILE_PROFILE`
- `CLIENTPLATFORM_E2E_VK_MOBILE_PROFILE`
- `CLIENTPLATFORM_E2E_MAX_MOBILE_PROFILE`
- `CLIENTPLATFORM_E2E_COCKPIT_MOBILE_PROFILE`

A native Appium profile identifies the already-installed application and its
test UI locators. Example shape:

```json
{
  "app_id": "provider.application.id",
  "chat_locator": {"using": "accessibility id", "value": "ClientPlatform E2E"},
  "composer_locator": {"using": "id", "value": "provider.input.id"},
  "send_locator": {"using": "accessibility id", "value": "Send"},
  "clear_before_input": true,
  "response_timeout_seconds": 30
}
```

The cockpit profile uses a browser session:

```json
{
  "browser_name": "Chrome",
  "url": "https://staging.example.invalid/",
  "response_timeout_seconds": 30
}
```

Use `Safari` for iPhone/iPad. Fire/ChromeOS profiles must describe the browser
actually installed on those dedicated devices.

Profiles must not contain passwords, tokens, cookies, authorization headers or
other credentials. Authentication lives only in the dedicated device profile.

## Evidence rules

The Appium driver does not accept “the screen changed” as success. For every
probe it requires both:

1. a changed UI-source SHA-256 fingerprint; and
2. a newly appearing occurrence of the expected ClientPlatform response text.

Only hashes, result metadata and screenshots are persisted. Raw UI source is not
stored.

The Hypium adapter follows the same rule indirectly: the runner-local DevEco
executor must return structured evidence for every expected probe with
`expected_text_asserted=true` and a screenshot file. Missing or extra probes,
an exit-code-only success, or missing evidence fails closed.

## Safety

The mobile contour inherits the Windows safety boundary:

- staging only;
- dedicated synthetic accounts and tenants only;
- no production credentials or customer data;
- no cross-product credentials;
- no real-money payment;
- paid AI/payment branches only in sandbox or explicit zero-cost test projects.

The repository is public. Treat every attached device and its host as an exposed
synthetic test worker, never as a production administration machine.
