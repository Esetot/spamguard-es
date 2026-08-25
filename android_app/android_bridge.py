from pathlib import Path
from kivy.utils import platform

IS_ANDROID = platform == "android"


def get_files_dir() -> Path:
    if IS_ANDROID:
        from jnius import autoclass
        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        return Path(str(PythonActivity.mActivity.getFilesDir().getAbsolutePath()))
    return Path.home() / ".spamguard_es"


def is_screening_role_held() -> bool:
    if not IS_ANDROID:
        return False
    from jnius import autoclass
    A = autoclass("org.kivy.android.PythonActivity")
    H = autoclass("org.spamguard.spamguardes.SpamGuardRoleHelper")
    return bool(H.isHeld(A.mActivity))


def request_screening_role() -> bool:
    if not IS_ANDROID:
        return False
    from jnius import autoclass
    A = autoclass("org.kivy.android.PythonActivity")
    H = autoclass("org.spamguard.spamguardes.SpamGuardRoleHelper")
    return bool(H.requestRole(A.mActivity))


def get_blocking_enabled() -> bool:
    if not IS_ANDROID:
        return True
    from jnius import autoclass
    A = autoclass("org.kivy.android.PythonActivity")
    P = autoclass("org.spamguard.spamguardes.SpamGuardPrefs")
    return bool(P.getBlockingEnabled(A.mActivity))


def set_blocking_enabled(value: bool) -> None:
    if IS_ANDROID:
        from jnius import autoclass
        A = autoclass("org.kivy.android.PythonActivity")
        P = autoclass("org.spamguard.spamguardes.SpamGuardPrefs")
        P.setBlockingEnabled(A.mActivity, bool(value))


def get_silence_review_enabled() -> bool:
    if not IS_ANDROID:
        return True
    from jnius import autoclass
    A = autoclass("org.kivy.android.PythonActivity")
    P = autoclass("org.spamguard.spamguardes.SpamGuardPrefs")
    return bool(P.getSilenceReviewEnabled(A.mActivity))


def set_silence_review_enabled(value: bool) -> None:
    if IS_ANDROID:
        from jnius import autoclass
        A = autoclass("org.kivy.android.PythonActivity")
        P = autoclass("org.spamguard.spamguardes.SpamGuardPrefs")
        P.setSilenceReviewEnabled(A.mActivity, bool(value))


def configure_native_updates(raw_base: str) -> None:
    if not IS_ANDROID:
        return
    from jnius import autoclass
    A = autoclass("org.kivy.android.PythonActivity")
    P = autoclass("org.spamguard.spamguardes.SpamGuardPrefs")
    S = autoclass("org.spamguard.spamguardes.SpamGuardUpdateScheduler")
    P.setRawBase(A.mActivity, raw_base.strip().rstrip("/"))
    S.schedule(A.mActivity)
