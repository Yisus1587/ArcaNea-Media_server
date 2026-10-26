package com.arcanea.media;

import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.Settings;

import androidx.core.content.ContextCompat;

import com.chaquo.python.Python;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * Bridge between the WebView and the phone: starts/stops the embedded server and
 * handles the "all files access" permission needed to read media from /sdcard.
 */
@CapacitorPlugin(name = "LocalServer")
public class LocalServerPlugin extends Plugin {

    @PluginMethod
    public void start(PluginCall call) {
        Integer requested = call.getInt("port", LocalServerService.DEFAULT_PORT);
        int port = requested == null ? LocalServerService.DEFAULT_PORT : requested;
        Context context = getContext();

        Intent intent = new Intent(context, LocalServerService.class);
        intent.setAction(LocalServerService.ACTION_START);
        intent.putExtra(LocalServerService.EXTRA_PORT, port);
        intent.putExtra(LocalServerService.EXTRA_DATA_DIR, LocalServerService.defaultDataDir(context));

        try {
            ContextCompat.startForegroundService(context, intent);
        } catch (Throwable error) {
            call.reject("No se pudo iniciar el servicio: " + error.getMessage());
            return;
        }

        JSObject result = new JSObject();
        result.put("port", port);
        result.put("origin", "http://127.0.0.1:" + port);
        result.put("running", LocalServerService.isRunning());
        call.resolve(result);
    }

    @PluginMethod
    public void stop(PluginCall call) {
        Context context = getContext();
        Intent intent = new Intent(context, LocalServerService.class);
        intent.setAction(LocalServerService.ACTION_STOP);
        try {
            context.startService(intent);
        } catch (Throwable error) {
            // The service may not be running; stopping is best effort.
        }
        JSObject result = new JSObject();
        result.put("running", false);
        call.resolve(result);
    }

    @PluginMethod
    public void status(PluginCall call) {
        JSObject result = new JSObject();
        result.put("running", LocalServerService.isRunning());
        result.put("port", LocalServerService.currentPort());
        result.put("allFiles", hasAllFilesAccess());
        result.put("platform", "android");
        call.resolve(result);
    }

    @PluginMethod
    public void requestAllFilesAccess(PluginCall call) {
        JSObject result = new JSObject();
        if (hasAllFilesAccess()) {
            result.put("granted", true);
            call.resolve(result);
            return;
        }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.R) {
            // Before Android 11 the legacy storage permission was enough; the app
            // declares it with maxSdkVersion=32 so the OS prompts by itself.
            result.put("granted", false);
            result.put("detail", "not_required");
            call.resolve(result);
            return;
        }

        try {
            Intent intent = new Intent(
                    Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                    Uri.parse("package:" + getContext().getPackageName())
            );
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(intent);
        } catch (Throwable error) {
            // Some OEM builds lack the per-app screen; fall back to the global list.
            try {
                Intent fallback = new Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION);
                fallback.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                getContext().startActivity(fallback);
            } catch (Throwable ignored) {
                call.reject("No se pudo abrir los ajustes de almacenamiento");
                return;
            }
        }

        result.put("granted", false);
        result.put("detail", "opened_settings");
        call.resolve(result);
    }

    private boolean hasAllFilesAccess() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                return Environment.isExternalStorageManager();
            }
            return true;
        } catch (Throwable error) {
            return false;
        }
    }

    /** Used by `status` to report whether Python actually came up. */
    @SuppressWarnings("unused")
    private boolean pythonRunning() {
        try {
            if (!Python.isStarted()) return false;
            return Python.getInstance().getModule("src.android_entry").callAttr("is_running").toBoolean();
        } catch (Throwable error) {
            return false;
        }
    }
}
