package com.arcanea.media;

import android.content.Intent;
import android.text.TextUtils;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import org.json.JSONArray;

/**
 * Opens the native player and forwards its state to the WebView.
 *
 * `play()` stays pending until the player activity closes and then resolves with
 * the final position, so the web app can store the resume point even if the
 * WebView was suspended while the video was on screen. Live progress is pushed
 * as `nativePlayerProgress` events in the meantime.
 */
@CapacitorPlugin(name = "NativePlayer")
public class NativePlayerPlugin extends Plugin {

    private static NativePlayerPlugin instance;

    /** The in-flight `play()` call; resolved when the activity finishes. */
    private PluginCall pending;

    @Override
    public void load() {
        instance = this;
    }

    @Override
    public void handleOnDestroy() {
        if (instance == this) {
            instance = null;
        }
        super.handleOnDestroy();
    }

    @PluginMethod
    public void available(PluginCall call) {
        JSObject result = new JSObject();
        result.put("available", true);
        result.put("engine", "exoplayer");
        call.resolve(result);
    }

    @PluginMethod
    public void play(PluginCall call) {
        JSArray items = call.getArray("items");
        if (items == null || items.length() == 0) {
            call.reject("La lista de reproducción está vacía.");
            return;
        }

        Integer startIndex = call.getInt("startIndex", 0);
        Double startPosition = call.getDouble("startPositionMs", 0d);
        String title = call.getString("title", "");
        String preferredLanguage = call.getString("preferredTextLanguage", "");
        Boolean showSubtitles = call.getBoolean("showSubtitles", Boolean.FALSE);

        Intent intent = new Intent(getContext(), NativePlayerActivity.class);
        intent.putExtra(NativePlayerActivity.EXTRA_ITEMS, items.toString());
        intent.putExtra(NativePlayerActivity.EXTRA_START_INDEX, startIndex == null ? 0 : startIndex);
        intent.putExtra(NativePlayerActivity.EXTRA_START_POSITION_MS,
                startPosition == null ? 0L : startPosition.longValue());
        intent.putExtra(NativePlayerActivity.EXTRA_TITLE, title);
        if (!TextUtils.isEmpty(preferredLanguage)) {
            intent.putExtra(NativePlayerActivity.EXTRA_PREFERRED_TEXT_LANGUAGE, preferredLanguage);
        }
        intent.putExtra(NativePlayerActivity.EXTRA_SHOW_SUBTITLES,
                showSubtitles != null && showSubtitles);

        call.setKeepAlive(Boolean.TRUE);
        pending = call;

        if (getActivity() == null) {
            pending = null;
            call.reject("La actividad no está disponible.");
            return;
        }

        getActivity().runOnUiThread(() -> {
            try {
                getContext().startActivity(intent);
            } catch (Throwable error) {
                PluginCall waiting = pending;
                pending = null;
                if (waiting != null) {
                    waiting.reject("No se pudo abrir el reproductor: " + error.getMessage());
                }
            }
        });
    }

    /** Called by the player activity every few seconds. */
    static void onPlayerProgress(int index, long positionMs, long durationMs) {
        NativePlayerPlugin plugin = instance;
        if (plugin == null) {
            return;
        }
        JSObject data = new JSObject();
        data.put("index", index);
        data.put("positionMs", positionMs);
        data.put("durationMs", durationMs);
        plugin.notifyListeners("nativePlayerProgress", data);
    }

    /** Called once, when the player activity closes (or fails). */
    static void onPlayerFinished(int index, long positionMs, long durationMs, boolean ended, String error) {
        NativePlayerPlugin plugin = instance;
        if (plugin == null) {
            return;
        }
        JSObject result = new JSObject();
        result.put("index", index);
        result.put("positionMs", positionMs);
        result.put("durationMs", durationMs);
        result.put("ended", ended);
        if (!TextUtils.isEmpty(error)) {
            result.put("error", error);
        }
        PluginCall waiting = plugin.pending;
        plugin.pending = null;
        if (waiting != null) {
            waiting.resolve(result);
        }
    }
}
