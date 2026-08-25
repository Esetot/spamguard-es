# Validation

Validated on 2026-08-25 before packaging:

- Python syntax: PASS (`main.py`, Android bridge/core, backend).
- Android core tests: 3/3 PASS.
- Backend reputation tests: 11/11 PASS.
- Backend empty-state export: PASS, including BLOCK/REVIEW files and mobile manifest.
- Native Java source: syntax/type-checked against minimal Android API stubs: PASS.
- GitHub Actions YAML parse: PASS.
- Buildozer configuration parse and native-source/manifest paths: PASS.

Not validated in this environment:

- A full Android SDK/NDK Buildozer APK compilation.
- Installation/runtime behavior on a physical Android device.
- Live scraping against all upstream websites.

Those three checks are intentionally delegated to the included GitHub Actions and the first device test.
