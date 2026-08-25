package org.spamguard.spamguardes;

import android.app.Activity;
import android.app.role.RoleManager;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

public final class SpamGuardRoleHelper {
    private static final int REQUEST_CALL_SCREENING_ROLE = 4242;
    private SpamGuardRoleHelper() {}

    public static boolean isHeld(Context context) {
        if (context == null || Build.VERSION.SDK_INT < 29) return false;
        RoleManager manager = (RoleManager) context.getSystemService(Context.ROLE_SERVICE);
        return manager != null && manager.isRoleAvailable(RoleManager.ROLE_CALL_SCREENING) && manager.isRoleHeld(RoleManager.ROLE_CALL_SCREENING);
    }

    public static boolean requestRole(Activity activity) {
        if (activity == null || Build.VERSION.SDK_INT < 29) return false;
        RoleManager manager = (RoleManager) activity.getSystemService(Context.ROLE_SERVICE);
        if (manager == null || !manager.isRoleAvailable(RoleManager.ROLE_CALL_SCREENING)) return false;
        if (manager.isRoleHeld(RoleManager.ROLE_CALL_SCREENING)) return true;
        Intent intent = manager.createRequestRoleIntent(RoleManager.ROLE_CALL_SCREENING);
        activity.startActivityForResult(intent, REQUEST_CALL_SCREENING_ROLE);
        return true;
    }
}
