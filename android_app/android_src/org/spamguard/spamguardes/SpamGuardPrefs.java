package org.spamguard.spamguardes;

import android.content.Context;
import android.content.SharedPreferences;

public final class SpamGuardPrefs {
    public static final String PREFS_NAME = "spamguard_settings";
    public static final String KEY_BLOCKING_ENABLED = "blocking_enabled";
    public static final String KEY_SILENCE_REVIEW_ENABLED = "silence_review_enabled";
    public static final String KEY_RAW_BASE = "raw_base";
    private SpamGuardPrefs() {}
    private static SharedPreferences prefs(Context context) { return context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE); }
    public static boolean getBlockingEnabled(Context context) { return prefs(context).getBoolean(KEY_BLOCKING_ENABLED, true); }
    public static void setBlockingEnabled(Context context, boolean value) { prefs(context).edit().putBoolean(KEY_BLOCKING_ENABLED, value).apply(); }
    public static boolean getSilenceReviewEnabled(Context context) { return prefs(context).getBoolean(KEY_SILENCE_REVIEW_ENABLED, true); }
    public static void setSilenceReviewEnabled(Context context, boolean value) { prefs(context).edit().putBoolean(KEY_SILENCE_REVIEW_ENABLED, value).apply(); }
    public static String getRawBase(Context context) { return prefs(context).getString(KEY_RAW_BASE, ""); }
    public static void setRawBase(Context context, String value) { prefs(context).edit().putString(KEY_RAW_BASE, value == null ? "" : value).apply(); }
}
