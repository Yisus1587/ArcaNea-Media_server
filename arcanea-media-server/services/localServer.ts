import { Capacitor, registerPlugin } from '@capacitor/core';

/**
 * Local server mode: the phone runs the backend itself (Python inside the APK) and
 * the WebView talks to 127.0.0.1. Nothing here exists on the web build; every call
 * is guarded by `localServerSupported()`.
 */

export type LocalServerStatus = {
  running: boolean;
  port: number;
  allFiles: boolean;
};

type LocalServerNative = {
  start(options: { port?: number }): Promise<{ port: number; origin: string; running: boolean }>;
  stop(): Promise<{ running: boolean }>;
  status(): Promise<LocalServerStatus & { platform?: string }>;
  requestAllFilesAccess(): Promise<{ granted: boolean; detail?: string }>;
};

const LocalServer = registerPlugin<LocalServerNative>('LocalServer');

export const LOCAL_SERVER_DEFAULT_PORT = 9800;

export function localServerSupported(): boolean {
  try {
    return Capacitor.isNativePlatform() && Capacitor.getPlatform() === 'android';
  } catch {
    return false;
  }
}

export async function readLocalServerStatus(): Promise<LocalServerStatus | null> {
  if (!localServerSupported()) return null;
  try {
    const res = await LocalServer.status();
    return { running: !!res.running, port: res.port || LOCAL_SERVER_DEFAULT_PORT, allFiles: !!res.allFiles };
  } catch {
    return null;
  }
}

export async function requestAllFilesAccess(): Promise<boolean> {
  if (!localServerSupported()) return false;
  try {
    const res = await LocalServer.requestAllFilesAccess();
    return !!res.granted;
  } catch {
    return false;
  }
}

async function waitForHealth(origin: string, timeoutMs: number): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 3000);
      const res = await fetch(`${origin}/api/health?providers=0`, { signal: controller.signal });
      clearTimeout(timer);
      if (res.ok) return true;
    } catch {
      // Server still booting (first launch runs the DB migrations).
    }
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
  return false;
}

/**
 * Start the embedded server and wait until `/api/health` answers.
 * Returns the origin to use, or null when the phone could not start it.
 */
export async function startLocalServer(
  port: number = LOCAL_SERVER_DEFAULT_PORT,
  timeoutMs: number = 45000,
): Promise<string | null> {
  if (!localServerSupported()) return null;
  const origin = `http://127.0.0.1:${port}`;

  const status = await readLocalServerStatus();
  if (status?.running && status.port === port) {
    return (await waitForHealth(origin, 8000)) ? origin : null;
  }

  try {
    await LocalServer.start({ port });
  } catch {
    return null;
  }
  return (await waitForHealth(origin, timeoutMs)) ? origin : null;
}

export async function stopLocalServer(): Promise<void> {
  if (!localServerSupported()) return;
  try {
    await LocalServer.stop();
  } catch {
    // ignore
  }
}
