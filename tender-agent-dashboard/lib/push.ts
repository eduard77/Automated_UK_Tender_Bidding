// Browser-side Web Push helpers. Backend subscribe/unsubscribe endpoints land in T3.
//
// Usage:
//   - isSubscribed() — check current PushSubscription state on the active SW.
//   - subscribePush(vapidPublicKey) — request permission, register SW, subscribe,
//     and POST the subscription to the dashboard's local /api/push/subscribe proxy.
//
// The proxy route at /api/push/subscribe is added in T3; until then the POST will
// 404 and subscribePush will throw.

const SW_PATH = "/sw.js";
const SUBSCRIBE_ENDPOINT = "/api/push/subscribe";

function assertBrowser(): void {
  if (typeof window === "undefined") {
    throw new Error("push: not in a browser");
  }
}

function pushSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

async function getRegistration(): Promise<ServiceWorkerRegistration> {
  assertBrowser();
  if (!pushSupported()) throw new Error("push: not supported in this browser");
  const existing = await navigator.serviceWorker.getRegistration();
  if (existing) return existing;
  return navigator.serviceWorker.register(SW_PATH);
}

export async function isSubscribed(): Promise<boolean> {
  if (!pushSupported()) return false;
  try {
    const reg = await navigator.serviceWorker.getRegistration();
    if (!reg) return false;
    const sub = await reg.pushManager.getSubscription();
    return sub !== null;
  } catch {
    return false;
  }
}

export async function subscribePush(vapidPublicKey: string): Promise<PushSubscription> {
  assertBrowser();
  if (!vapidPublicKey) throw new Error("push: missing VAPID public key");
  if (!pushSupported()) throw new Error("push: not supported in this browser");

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error(`push: permission ${permission}`);
  }

  const reg = await getRegistration();
  await navigator.serviceWorker.ready;

  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
    });
  }

  const res = await fetch(SUBSCRIBE_ENDPOINT, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sub.toJSON()),
  });
  if (!res.ok) {
    throw new Error(`push: subscribe failed (${res.status})`);
  }

  return sub;
}

export async function unsubscribePush(): Promise<boolean> {
  if (!pushSupported()) return false;
  const reg = await navigator.serviceWorker.getRegistration();
  if (!reg) return false;
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return false;
  return sub.unsubscribe();
}

// VAPID keys are url-safe base64; the PushManager wants a BufferSource backed
// by a plain ArrayBuffer (not SharedArrayBuffer), so allocate one explicitly.
function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const normalised = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(normalised);
  const buf = new ArrayBuffer(raw.length);
  const out = new Uint8Array(buf);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}
