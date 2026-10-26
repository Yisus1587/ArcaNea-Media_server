package com.arcanea.media;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import androidx.annotation.Nullable;
import androidx.core.app.NotificationCompat;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/**
 * Runs the ArcaNea backend (uvicorn on 127.0.0.1) while the app is in the background.
 *
 * Android kills plain background threads, so the server lives inside a foreground
 * service with a notification. The Python side is `src.android_entry`, which starts
 * uvicorn in its own thread and returns `ok:<port>` when the socket is up.
 */
public class LocalServerService extends Service {

    public static final String ACTION_START = "com.arcanea.media.action.LOCAL_SERVER_START";
    public static final String ACTION_STOP = "com.arcanea.media.action.LOCAL_SERVER_STOP";
    public static final String EXTRA_PORT = "port";
    public static final String EXTRA_DATA_DIR = "dataDir";

    private static final String TAG = "ArcaNeaLocalServer";
    private static final String CHANNEL_ID = "arcanea_local_server";
    private static final int NOTIFICATION_ID = 4711;
    public static final int DEFAULT_PORT = 9800;

    private static volatile boolean running = false;
    private static volatile int port = DEFAULT_PORT;

    public static boolean isRunning() {
        return running;
    }

    public static int currentPort() {
        return port;
    }

    @Nullable
    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopServer();
            return START_NOT_STICKY;
        }

        int requestedPort = DEFAULT_PORT;
        String dataDir = null;
        if (intent != null) {
            requestedPort = intent.getIntExtra(EXTRA_PORT, DEFAULT_PORT);
            dataDir = intent.getStringExtra(EXTRA_DATA_DIR);
        }
        if (dataDir == null || dataDir.isEmpty()) {
            dataDir = defaultDataDir(this);
        }
        port = requestedPort;

        startForeground(NOTIFICATION_ID, buildNotification(requestedPort));

        final String startDataDir = dataDir;
        final int startPort = requestedPort;
        new Thread(() -> {
            try {
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(getApplicationContext()));
                }
                PyObject entry = Python.getInstance().getModule("src.android_entry");
                PyObject result = entry.callAttr("start", startPort, startDataDir);
                String status = result == null ? "" : result.toString();
                Log.i(TAG, "python start -> " + status);
                running = status.startsWith("ok:") || status.startsWith("already_running:");
            } catch (Throwable error) {
                running = false;
                Log.e(TAG, "local server failed to start", error);
            }
        }, "arcanea-python-start").start();

        return START_STICKY;
    }

    private void stopServer() {
        try {
            if (Python.isStarted()) {
                PyObject entry = Python.getInstance().getModule("src.android_entry");
                PyObject result = entry.callAttr("stop");
                Log.i(TAG, "python stop -> " + (result == null ? "" : result.toString()));
            }
        } catch (Throwable error) {
            Log.w(TAG, "error stopping python server", error);
        } finally {
            running = false;
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                    stopForeground(STOP_FOREGROUND_REMOVE);
                } else {
                    stopForeground(true);
                }
            } catch (Throwable ignored) {
                // best effort
            }
            stopSelf();
        }
    }

    @Override
    public void onDestroy() {
        try {
            if (Python.isStarted()) {
                Python.getInstance().getModule("src.android_entry").callAttr("stop");
            }
        } catch (Throwable error) {
            Log.w(TAG, "error stopping python server on destroy", error);
        }
        running = false;
        super.onDestroy();
    }

    public static String defaultDataDir(Context context) {
        // App-private storage: survives updates and needs no storage permission.
        return context.getFilesDir().getAbsolutePath() + "/arcanea";
    }

    private Notification buildNotification(int notifPort) {
        NotificationManager manager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && manager != null) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID,
                    "ArcaNea local server",
                    NotificationManager.IMPORTANCE_LOW
            );
            channel.setDescription("Keeps the ArcaNea server running on this phone");
            manager.createNotificationChannel(channel);
        }

        Intent openIntent = new Intent(this, MainActivity.class);
        openIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent openPending = PendingIntent.getActivity(
                this,
                0,
                openIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        Intent stopIntent = new Intent(this, LocalServerService.class);
        stopIntent.setAction(ACTION_STOP);
        PendingIntent stopPending = PendingIntent.getService(
                this,
                1,
                stopIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        return new NotificationCompat.Builder(this, CHANNEL_ID)
                .setContentTitle("ArcaNea")
                .setContentText("Servidor local en 127.0.0.1:" + notifPort)
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setOngoing(true)
                .setPriority(NotificationCompat.PRIORITY_LOW)
                .setContentIntent(openPending)
                .addAction(0, "Detener", stopPending)
                .build();
    }
}
