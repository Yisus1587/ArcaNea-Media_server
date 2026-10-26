package com.arcanea.media;

import android.app.Activity;
import android.app.Dialog;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.Nullable;
import androidx.media3.common.AudioAttributes;
import androidx.media3.common.C;
import androidx.media3.common.MediaItem;
import androidx.media3.common.MediaMetadata;
import androidx.media3.common.PlaybackException;
import androidx.media3.common.Player;
import androidx.media3.exoplayer.ExoPlayer;
import androidx.media3.exoplayer.trackselection.DefaultTrackSelector;
import androidx.media3.ui.PlayerView;
import androidx.media3.ui.TrackSelectionDialogBuilder;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * Full-screen Media3 (ExoPlayer) player used by the Android app.
 *
 * The WebView's own <video> element cannot decode Matroska or HEVC on most
 * phones, so the web player hands playback over to this activity, which plays
 * the same `GET /api/stream?path=...` URL (ExoPlayer issues byte ranges) and
 * renders embedded MKV subtitle tracks plus the sidecar `.srt`/`.vtt` files
 * listed by `/api/subtitles/tracks`.
 *
 * State travels back through {@link NativePlayerPlugin}: progress events while
 * playing, and one final resolve with position/duration/index when the activity
 * closes, so the web app can store the resume point.
 */
public class NativePlayerActivity extends Activity {

    public static final String EXTRA_ITEMS = "items";
    public static final String EXTRA_START_INDEX = "startIndex";
    public static final String EXTRA_START_POSITION_MS = "startPositionMs";
    public static final String EXTRA_TITLE = "title";
    public static final String EXTRA_PREFERRED_TEXT_LANGUAGE = "preferredTextLanguage";
    public static final String EXTRA_SHOW_SUBTITLES = "showSubtitles";

    private static final long PROGRESS_INTERVAL_MS = 10_000L;

    @Nullable
    private ExoPlayer player;
    @Nullable
    private PlayerView playerView;
    @Nullable
    private DefaultTrackSelector trackSelector;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private boolean finalPublished;
    private boolean started;

    private final Runnable progressTick = new Runnable() {
        @Override
        public void run() {
            publishProgress();
            handler.postDelayed(this, PROGRESS_INTERVAL_MS);
        }
    };

    @Override
    protected void onCreate(@Nullable Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // Keep the screen on while a movie is playing; the web overlay is hidden.
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        Intent intent = getIntent();
        String itemsJson = intent.getStringExtra(EXTRA_ITEMS);
        String title = intent.getStringExtra(EXTRA_TITLE);
        String preferredLanguage = intent.getStringExtra(EXTRA_PREFERRED_TEXT_LANGUAGE);
        boolean showSubtitles = intent.getBooleanExtra(EXTRA_SHOW_SUBTITLES, false);
        int startIndex = intent.getIntExtra(EXTRA_START_INDEX, 0);
        long startPositionMs = Math.max(0L, intent.getLongExtra(EXTRA_START_POSITION_MS, 0L));

        List<MediaItem> mediaItems;
        try {
            mediaItems = buildMediaItems(itemsJson);
        } catch (Exception error) {
            finishWithError("Lista de reproducción inválida: " + error.getMessage());
            return;
        }
        if (mediaItems.isEmpty()) {
            finishWithError("No hay nada que reproducir.");
            return;
        }
        int safeIndex = Math.max(0, Math.min(startIndex, mediaItems.size() - 1));

        setContentView(buildContentView(title, safeIndex, mediaItems.size()));

        trackSelector = new DefaultTrackSelector(this);
        if (showSubtitles) {
            DefaultTrackSelector.Parameters.Builder params = trackSelector.getParameters().buildUpon();
            if (!TextUtils.isEmpty(preferredLanguage)) {
                params.setPreferredTextLanguage(preferredLanguage);
            }
            // With no language hint, still prefer a track the file marks as default.
            params.setSelectUndeterminedTextLanguage(showSubtitles);
            trackSelector.setParameters(params.build());
        }

        AudioAttributes audioAttributes = new AudioAttributes.Builder()
                .setUsage(C.USAGE_MEDIA)
                .setContentType(C.AUDIO_CONTENT_TYPE_MOVIE)
                .build();

        player = new ExoPlayer.Builder(this)
                .setTrackSelector(trackSelector)
                .setAudioAttributes(audioAttributes, /* handleAudioFocus= */ true)
                .build();

        if (playerView != null) {
            playerView.setPlayer(player);
        }

        player.addListener(new Player.Listener() {
            @Override
            public void onPlaybackStateChanged(int playbackState) {
                if (playbackState == Player.STATE_ENDED) {
                    // ExoPlayer advances through the playlist; only the last item ends here.
                    publishProgress();
                } else if (playbackState == Player.STATE_READY) {
                    started = true;
                }
            }

            @Override
            public void onPositionDiscontinuity(
                    Player.PositionInfo oldPosition,
                    Player.PositionInfo newPosition,
                    int reason) {
                // Episode change (or a seek): report so the right file gets the mark.
                publishProgress();
            }

            @Override
            public void onPlayerError(PlaybackException error) {
                String detail = error.getErrorCodeName();
                if (!TextUtils.isEmpty(error.getMessage())) {
                    detail = detail + ": " + error.getMessage();
                }
                finishWithError("No se pudo reproducir el archivo (" + detail + ").");
            }
        });

        player.setMediaItems(mediaItems, safeIndex, startPositionMs);
        player.prepare();
        player.setPlayWhenReady(true);
    }

    private void finishWithError(String message) {
        publishFinal(false, message);
        Toast.makeText(this, message, Toast.LENGTH_LONG).show();
        finish();
    }

    /**
     * The playlist arrives as JSON so the bridge does not need a Parcelable:
     * [{ "url": ..., "title": ..., "subtitles": [{ "url", "mime", "label", "language" }] }]
     */
    private List<MediaItem> buildMediaItems(@Nullable String json) throws Exception {
        List<MediaItem> result = new ArrayList<>();
        if (TextUtils.isEmpty(json)) {
            return result;
        }
        JSONArray array = new JSONArray(json);
        for (int i = 0; i < array.length(); i++) {
            JSONObject entry = array.optJSONObject(i);
            if (entry == null) {
                continue;
            }
            String url = entry.optString("url", "");
            if (TextUtils.isEmpty(url)) {
                continue;
            }
            String itemTitle = entry.optString("title", "");
            MediaItem.Builder builder = new MediaItem.Builder()
                    .setUri(url)
                    .setMediaMetadata(new MediaMetadata.Builder().setTitle(itemTitle).build());

            JSONArray subtitles = entry.optJSONArray("subtitles");
            if (subtitles != null && subtitles.length() > 0) {
                List<MediaItem.SubtitleConfiguration> configurations = new ArrayList<>();
                for (int s = 0; s < subtitles.length(); s++) {
                    JSONObject track = subtitles.optJSONObject(s);
                    if (track == null) {
                        continue;
                    }
                    String trackUrl = track.optString("url", "");
                    if (TextUtils.isEmpty(trackUrl)) {
                        continue;
                    }
                    MediaItem.SubtitleConfiguration.Builder subtitle =
                            new MediaItem.SubtitleConfiguration.Builder(android.net.Uri.parse(trackUrl))
                                    .setMimeType(track.optString("mime", "application/x-subrip"))
                                    .setLabel(track.optString("label", "Subtítulos"));
                    String language = track.optString("language", "");
                    if (!TextUtils.isEmpty(language)) {
                        subtitle.setLanguage(language);
                    }
                    if (track.optBoolean("default", false)) {
                        subtitle.setSelectionFlags(C.SELECTION_FLAG_DEFAULT);
                    }
                    configurations.add(subtitle.build());
                }
                if (!configurations.isEmpty()) {
                    builder.setSubtitleConfigurations(configurations);
                }
            }
            result.add(builder.build());
        }
        return result;
    }

    private View buildContentView(@Nullable String title, int startIndex, int total) {
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        playerView = new PlayerView(this);
        playerView.setShowBuffering(PlayerView.SHOW_BUFFERING_WHEN_PLAYING);
        playerView.setControllerShowTimeoutMs(3500);
        root.addView(playerView, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));

        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setBackgroundColor(0xAA000000);
        bar.setPadding(dp(16), dp(10), dp(10), dp(10));

        TextView titleView = new TextView(this);
        titleView.setTextColor(Color.WHITE);
        titleView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15);
        titleView.setTypeface(Typeface.DEFAULT_BOLD);
        titleView.setSingleLine(true);
        titleView.setEllipsize(TextUtils.TruncateAt.END);
        String heading = TextUtils.isEmpty(title) ? "Reproduciendo" : title;
        if (total > 1) {
            heading = heading + "  ·  " + (startIndex + 1) + "/" + total;
        }
        titleView.setText(heading);
        LinearLayout.LayoutParams titleParams = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
        bar.addView(titleView, titleParams);

        bar.addView(chromeButton("Audio", view -> showTrackDialog(C.TRACK_TYPE_AUDIO, "Audio")));
        bar.addView(chromeButton("Subs", view -> showTrackDialog(C.TRACK_TYPE_TEXT, "Subtítulos")));
        bar.addView(chromeButton("Cerrar", view -> finish()));

        FrameLayout.LayoutParams barParams = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT,
                Gravity.TOP);
        root.addView(bar, barParams);
        return root;
    }

    private Button chromeButton(String text, View.OnClickListener listener) {
        Button button = new Button(this);
        button.setText(text);
        button.setAllCaps(false);
        button.setTextSize(TypedValue.COMPLEX_UNIT_SP, 13);
        button.setTextColor(Color.WHITE);
        button.setBackgroundColor(Color.TRANSPARENT);
        button.setOnClickListener(listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        params.setMargins(dp(4), 0, dp(4), 0);
        button.setLayoutParams(params);
        return button;
    }

    private void showTrackDialog(int trackType, String dialogTitle) {
        if (player == null) {
            return;
        }
        try {
            Dialog dialog = new TrackSelectionDialogBuilder(this, dialogTitle, player, trackType)
                    .setAllowAdaptiveSelections(false)
                    .setShowDisableOption(trackType == C.TRACK_TYPE_TEXT)
                    .build();
            dialog.show();
        } catch (Exception error) {
            Toast.makeText(this, "No hay pistas disponibles.", Toast.LENGTH_SHORT).show();
        }
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private long normalizedDuration() {
        if (player == null) {
            return 0L;
        }
        long duration = player.getDuration();
        return duration == C.TIME_UNSET || duration < 0 ? 0L : duration;
    }

    private void publishProgress() {
        if (player == null || finalPublished) {
            return;
        }
        long position = Math.max(0L, player.getCurrentPosition());
        NativePlayerPlugin.onPlayerProgress(player.getCurrentMediaItemIndex(), position, normalizedDuration());
    }

    private void publishFinal(boolean ended, @Nullable String error) {
        if (finalPublished) {
            return;
        }
        finalPublished = true;
        long position = player == null ? 0L : Math.max(0L, player.getCurrentPosition());
        int index = player == null ? 0 : player.getCurrentMediaItemIndex();
        NativePlayerPlugin.onPlayerFinished(index, position, normalizedDuration(), ended, error);
    }

    @Override
    protected void onPause() {
        // A phone call, the notification shade or the user leaving: store progress now.
        publishProgress();
        handler.removeCallbacks(progressTick);
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.removeCallbacks(progressTick);
        handler.postDelayed(progressTick, PROGRESS_INTERVAL_MS);
    }

    @Override
    public void finish() {
        // Covers the back button and the "Cerrar" button with the same path.
        publishFinal(false, null);
        super.finish();
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(progressTick);
        if (!finalPublished) {
            publishFinal(false, null);
        }
        if (playerView != null) {
            playerView.setPlayer(null);
        }
        if (player != null) {
            player.release();
            player = null;
        }
        super.onDestroy();
    }
}
