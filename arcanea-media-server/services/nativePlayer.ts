import { Capacitor, registerPlugin } from '@capacitor/core';

/**
 * Bridge to the Android player (Media3/ExoPlayer).
 *
 * The WebView's `<video>` cannot decode Matroska or HEVC on most phones, so
 * playback is handed to a native activity that plays the same
 * `GET /api/stream?path=...` URL the web player uses. Everything here is a
 * no-op on the web build, where the `<video>` element is the only player.
 */

export interface NativeSubtitleTrack {
  url: string;
  mime: string;
  label: string;
  language?: string;
  default?: boolean;
}

export interface NativePlayerItem {
  /** Absolute stream URL: the native player runs outside the WebView origin. */
  url: string;
  title: string;
  /** Library path, echoed back with progress so the right file gets the resume mark. */
  path?: string;
  subtitles?: NativeSubtitleTrack[];
}

export interface NativePlayerProgress {
  index: number;
  positionMs: number;
  durationMs: number;
}

export interface NativePlayerResult extends NativePlayerProgress {
  ended: boolean;
  error?: string;
}

interface NativePlayerPlugin {
  available(): Promise<{ available: boolean; engine: string }>;
  play(options: {
    items: NativePlayerItem[];
    startIndex?: number;
    startPositionMs?: number;
    title?: string;
    preferredTextLanguage?: string;
    showSubtitles?: boolean;
  }): Promise<NativePlayerResult>;
  addListener(
    eventName: 'nativePlayerProgress',
    listener: (event: NativePlayerProgress) => void,
  ): Promise<{ remove: () => void }>;
}

const plugin = registerPlugin<NativePlayerPlugin>('NativePlayer');

/** True only inside the Android app; the web build always uses `<video>`. */
export function isNativePlayerAvailable(): boolean {
  try {
    return Capacitor.isNativePlatform() && Capacitor.getPlatform() === 'android';
  } catch {
    return false;
  }
}

let availabilityProbe: Promise<boolean> | null = null;

/** Ask the native side once whether it can play (cached). */
export async function probeNativePlayer(): Promise<boolean> {
  if (!isNativePlayerAvailable()) return false;
  if (!availabilityProbe) {
    availabilityProbe = plugin
      .available()
      .then((result) => !!result?.available)
      .catch(() => false);
  }
  return availabilityProbe;
}

/** Listen to progress while the native player is in front; returns an unsubscribe. */
export async function addNativeProgressListener(
  listener: (event: NativePlayerProgress) => void,
): Promise<() => void> {
  if (!isNativePlayerAvailable()) return () => {};
  try {
    const handle = await plugin.addListener('nativePlayerProgress', listener);
    return () => {
      try {
        void handle.remove();
      } catch {
        // ignore
      }
    };
  } catch {
    return () => {};
  }
}

/**
 * Opens the native player and resolves when it closes.
 *
 * The promise stays pending for the whole playback session because the WebView
 * may be suspended while the activity is in front: resolving at the end is the
 * only reliable way to get the final position back.
 */
export async function playNative(options: {
  items: NativePlayerItem[];
  startIndex?: number;
  startPositionMs?: number;
  title?: string;
  preferredTextLanguage?: string;
  showSubtitles?: boolean;
}): Promise<NativePlayerResult> {
  if (!isNativePlayerAvailable()) {
    throw new Error('native player unavailable');
  }
  return plugin.play({
    items: options.items,
    startIndex: options.startIndex ?? 0,
    startPositionMs: Math.max(0, Math.round(options.startPositionMs ?? 0)),
    title: options.title,
    preferredTextLanguage: options.preferredTextLanguage,
    showSubtitles: !!options.showSubtitles,
  });
}
