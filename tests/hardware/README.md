# Hardware tests

Hardware tests are intentionally excluded from normal CI and replay verification. Read `docs/HARDWARE_VALIDATION.md`, close other camera owners, then explicitly opt in:

```powershell
$env:SPA_RUN_HARDWARE_TESTS = "1"
& .\scripts\verify.ps1 -Hardware
```

Enumeration does not acquire a device. The sustained stream test additionally requires `$env:SPA_HARDWARE_ALLOW_CONNECT = "1"` because it takes exclusive ownership. Record device/OS/driver/app versions with results. A skip is unvalidated hardware, not a pass.
