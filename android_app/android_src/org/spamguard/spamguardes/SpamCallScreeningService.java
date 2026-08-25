package org.spamguard.spamguardes;

import android.content.SharedPreferences;
import android.net.Uri;
import android.telecom.Call;
import android.telecom.CallScreeningService;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.util.Collections;
import java.util.HashSet;
import java.util.Set;

public class SpamCallScreeningService extends CallScreeningService {
    private static final Object CACHE_LOCK = new Object();
    private static volatile Set<String> blockCache = Collections.emptySet();
    private static volatile Set<String> reviewCache = Collections.emptySet();
    private static volatile long blockLastModified = Long.MIN_VALUE;
    private static volatile long reviewLastModified = Long.MIN_VALUE;

    @Override
    public void onScreenCall(Call.Details callDetails) {
        CallResponse.Builder response = new CallResponse.Builder();
        try {
            if (callDetails.getCallDirection() != Call.Details.DIRECTION_INCOMING) {
                respondToCall(callDetails, response.build());
                return;
            }
            Uri handle = callDetails.getHandle();
            if (handle == null || !"tel".equalsIgnoreCase(handle.getScheme())) {
                respondToCall(callDetails, response.build());
                return;
            }
            String phone = normalizePhone(handle.getSchemeSpecificPart());
            if (phone == null) {
                respondToCall(callDetails, response.build());
                return;
            }
            ensureCaches();
            SharedPreferences prefs = getSharedPreferences(SpamGuardPrefs.PREFS_NAME, MODE_PRIVATE);
            boolean blockingEnabled = prefs.getBoolean(SpamGuardPrefs.KEY_BLOCKING_ENABLED, true);
            boolean silenceReviewEnabled = prefs.getBoolean(SpamGuardPrefs.KEY_SILENCE_REVIEW_ENABLED, true);
            if (blockingEnabled && blockCache.contains(phone)) {
                response.setDisallowCall(true).setRejectCall(true).setSkipNotification(true);
            } else if (silenceReviewEnabled && reviewCache.contains(phone)) {
                response.setSilenceCall(true);
            }
        } catch (Throwable ignored) {
            // Fail open: any internal error must allow the call.
        }
        respondToCall(callDetails, response.build());
    }

    private void ensureCaches() {
        File block = new File(getFilesDir(), "blocklist.txt");
        File review = new File(getFilesDir(), "reviewlist.txt");
        long bm = block.exists() ? block.lastModified() : -1L;
        long rm = review.exists() ? review.lastModified() : -1L;
        if (bm == blockLastModified && rm == reviewLastModified) return;
        synchronized (CACHE_LOCK) {
            bm = block.exists() ? block.lastModified() : -1L;
            rm = review.exists() ? review.lastModified() : -1L;
            if (bm != blockLastModified) {
                blockCache = loadNumbers(block);
                blockLastModified = bm;
            }
            if (rm != reviewLastModified) {
                reviewCache = loadNumbers(review);
                reviewLastModified = rm;
            }
        }
    }

    private static Set<String> loadNumbers(File file) {
        if (!file.exists()) return Collections.emptySet();
        HashSet<String> numbers = new HashSet<>();
        try (BufferedReader reader = new BufferedReader(new FileReader(file))) {
            String line;
            while ((line = reader.readLine()) != null) {
                String phone = normalizePhone(line);
                if (phone != null) numbers.add(phone);
            }
        } catch (Exception ignored) {
            return Collections.emptySet();
        }
        return Collections.unmodifiableSet(numbers);
    }

    static String normalizePhone(String raw) {
        if (raw == null) return null;
        String digits = raw.replaceAll("\\D", "");
        if (digits.startsWith("0034") && digits.length() == 13) digits = digits.substring(4);
        else if (digits.startsWith("34") && digits.length() == 11) digits = digits.substring(2);
        if (digits.length() != 9) return null;
        char first = digits.charAt(0);
        if (first != '6' && first != '7' && first != '8' && first != '9') return null;
        return digits;
    }
}
