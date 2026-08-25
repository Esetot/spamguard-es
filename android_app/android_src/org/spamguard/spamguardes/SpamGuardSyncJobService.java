package org.spamguard.spamguardes;

import android.app.job.JobParameters;
import android.app.job.JobService;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.Collections;
import java.util.HashSet;
import java.util.Set;

public class SpamGuardSyncJobService extends JobService {
    @Override
    public boolean onStartJob(JobParameters params) {
        Thread worker = new Thread(() -> {
            boolean retry = false;
            try {
                syncNow();
            } catch (Exception ignored) {
                retry = true;
            }
            jobFinished(params, retry);
        }, "spamguard-native-sync");
        worker.start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        return true;
    }

    private void syncNow() throws Exception {
        String base = SpamGuardPrefs.getRawBase(this);
        if (base == null || base.isEmpty() || !base.startsWith("https://")) {
            return;
        }
        while (base.endsWith("/")) base = base.substring(0, base.length() - 1);

        byte[] manifestBytes = download(base + "/mobile_manifest.json");
        JSONObject manifest = new JSONObject(new String(manifestBytes, StandardCharsets.UTF_8));
        if (manifest.optInt("schema_version", 0) != 1) {
            throw new IllegalStateException("unsupported mobile manifest");
        }

        JSONObject files = manifest.getJSONObject("files");
        JSONObject blockInfo = files.getJSONObject("block");
        JSONObject reviewInfo = files.getJSONObject("review");

        byte[] blockBytes = download(base + "/" + blockInfo.getString("name"));
        byte[] reviewBytes = download(base + "/" + reviewInfo.getString("name"));

        if (!sha256(blockBytes).equalsIgnoreCase(blockInfo.getString("sha256"))) {
            throw new IllegalStateException("block sha256 mismatch");
        }
        if (!sha256(reviewBytes).equalsIgnoreCase(reviewInfo.getString("sha256"))) {
            throw new IllegalStateException("review sha256 mismatch");
        }

        Set<String> blocks = parseNumbers(blockBytes);
        Set<String> reviews = parseNumbers(reviewBytes);
        Set<String> overlap = new HashSet<>(blocks);
        overlap.retainAll(reviews);
        if (!overlap.isEmpty()) {
            throw new IllegalStateException("BLOCK/REVIEW overlap");
        }

        writeNumbersAtomic(new File(getFilesDir(), "blocklist.txt"), blocks);
        writeNumbersAtomic(new File(getFilesDir(), "reviewlist.txt"), reviews);

        JSONObject meta = new JSONObject();
        meta.put("last_sync", Instant.now().toString());
        meta.put("generated_at", manifest.opt("generated_at"));
        meta.put("block_count", blocks.size());
        meta.put("review_count", reviews.size());
        writeBytesAtomic(new File(getFilesDir(), "sync_meta.json"), (meta.toString(2) + "\n").getBytes(StandardCharsets.UTF_8));
    }

    private static byte[] download(String address) throws Exception {
        URL url = new URL(address);
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setConnectTimeout(12000);
        connection.setReadTimeout(15000);
        connection.setRequestMethod("GET");
        connection.setRequestProperty("User-Agent", "SpamGuardES/0.1 AndroidNative");
        connection.setRequestProperty("Accept", "application/json,text/plain;q=0.9,*/*;q=0.1");
        connection.setUseCaches(false);
        int code = connection.getResponseCode();
        if (code != 200) {
            connection.disconnect();
            throw new IllegalStateException("HTTP " + code);
        }
        try (InputStream in = connection.getInputStream(); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) != -1) out.write(buf, 0, n);
            return out.toByteArray();
        } finally {
            connection.disconnect();
        }
    }

    private static Set<String> parseNumbers(byte[] data) throws Exception {
        String preview = new String(data, StandardCharsets.UTF_8).toLowerCase();
        if (preview.contains("<html") || preview.contains("<!doctype")) {
            throw new IllegalStateException("HTML received instead of list");
        }
        HashSet<String> result = new HashSet<>();
        try (BufferedReader reader = new BufferedReader(new InputStreamReader(new ByteArrayInputStream(data), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty() || line.startsWith("#")) continue;
                String phone = SpamCallScreeningService.normalizePhone(line);
                if (phone == null) throw new IllegalStateException("invalid phone line");
                result.add(phone);
            }
        }
        return result;
    }

    private static String sha256(byte[] data) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] hash = digest.digest(data);
        StringBuilder sb = new StringBuilder(hash.length * 2);
        for (byte b : hash) sb.append(String.format("%02x", b & 0xff));
        return sb.toString();
    }

    private static void writeNumbersAtomic(File target, Set<String> numbers) throws Exception {
        java.util.ArrayList<String> sorted = new java.util.ArrayList<>(numbers);
        Collections.sort(sorted);
        StringBuilder sb = new StringBuilder();
        for (String n : sorted) sb.append(n).append('\n');
        writeBytesAtomic(target, sb.toString().getBytes(StandardCharsets.UTF_8));
    }

    private static void writeBytesAtomic(File target, byte[] data) throws Exception {
        File tmp = new File(target.getParentFile(), target.getName() + ".tmp");
        try (FileOutputStream out = new FileOutputStream(tmp)) {
            out.write(data);
            out.flush();
            out.getFD().sync();
        }
        try {
            Files.move(tmp.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } catch (java.nio.file.AtomicMoveNotSupportedException ex) {
            Files.move(tmp.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING);
        }
    }
}
