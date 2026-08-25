package org.spamguard.spamguardes;

import android.app.job.JobInfo;
import android.app.job.JobScheduler;
import android.content.ComponentName;
import android.content.Context;

public final class SpamGuardUpdateScheduler {
    private static final int JOB_ID = 34001;
    private static final long INTERVAL_MS = 12L * 60L * 60L * 1000L;
    private SpamGuardUpdateScheduler() {}

    public static boolean schedule(Context context) {
        if (context == null) return false;
        JobScheduler scheduler = (JobScheduler) context.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        if (scheduler == null) return false;
        JobInfo info = new JobInfo.Builder(JOB_ID, new ComponentName(context, SpamGuardSyncJobService.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPersisted(true)
                .setPeriodic(INTERVAL_MS)
                .build();
        return scheduler.schedule(info) == JobScheduler.RESULT_SUCCESS;
    }
}
