from pathlib import Path

SERVICE_XML = """
<service
    android:name="org.spamguard.spamguardes.SpamCallScreeningService"
    android:permission="android.permission.BIND_SCREENING_SERVICE"
    android:exported="true">
    <intent-filter>
        <action android:name="android.telecom.CallScreeningService" />
    </intent-filter>
</service>

<service
    android:name="org.spamguard.spamguardes.SpamGuardSyncJobService"
    android:permission="android.permission.BIND_JOB_SERVICE"
    android:exported="false" />
"""


def after_apk_build(toolchain):
    manifest_file = (
        Path(toolchain._dist.dist_dir)
        / "src"
        / "main"
        / "AndroidManifest.xml"
    )

    manifest = manifest_file.read_text(encoding="utf-8")

    if "SpamCallScreeningService" in manifest:
        print("SpamGuard services already present in AndroidManifest.xml")
        return

    if "</application>" not in manifest:
        raise RuntimeError(
            "No se encontró </application> en AndroidManifest.xml"
        )

    manifest = manifest.replace(
        "</application>",
        SERVICE_XML + "\n</application>",
        1,
    )

    manifest_file.write_text(
        manifest,
        encoding="utf-8",
    )

    print("SpamGuard services added to AndroidManifest.xml")
