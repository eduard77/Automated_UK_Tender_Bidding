// Browser-side Web Push helpers.
//
// All backend traffic goes through the dashboard's own API routes (under
// /api/push/*) which forward to the FastAPI backend — that keeps CORS simple
// and lets us evolve auth/headers in one place later.
//
// VAPID public key comes from /api/push/vapid-key (proxied + cached) so the key
// is never baked into the build. The private key is backend-only.

const SW_PATH = "/sw.js";
const SUBSCRIBE_PROXY = "/api/push/subscribe";
const UNSUBSCRIBE_PROXY = "/api/push/unsubscribe";
const VAPID_KEY_PROXY = "/api/push/vapid-key";

export interface VapidKey {
  public_key: string;
  subject: string;
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
  if (!pushSupported()) throw new Error("push: not supported in this browser");
  const existing = await navigator.serviceWorker.getRegistration();
  if (existing) return existing;
  return navigator.serviceWorker.register(SW_PATH);
}

// Public — used by the layout / bell to decide whether to render the button.
export async function getVapidPublicKey(): Promise<string | null> {
  try {
    const res = await fetch(VAPID_KEY_PROXY, { cache: "force-cache" });
    if (!res.ok) return null;
    const data = (await res.json()) as VapidKey;
    return data.public_key || null;
  } catch {
    return null;
  }
}

// True iff there is an active browser push subscription on this device.
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

// Subscribe this browser. Pass a VAPID public key fetched from /api/push/vapid-key.
// On success, the subscription has been POSTed to the backend and the browser
// will start receiving pushes for matched tenders.
export async function subscribePush(vapidPublicKey: string): Promise<PushSubscription> {
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

  const res = await fetch(SUBSCRIBE_PROXY, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sub.toJSON()),
  });
  if (!res.ok) {
    throw new Error(`push: subscribe failed (${res.status})`);
  }
  return sub;
}

// Unsubscribe — drops the browser subscription AND tells the backend to delete
// the record so we don't keep sending to a dead endpoint. Idempotent.
export async function unsubscribePush(): Promise<boolean> {
  if (!pushSupported()) return false;
  const reg = await navigator.serviceWorker.getRegistration();
  if (!reg) return false;
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return false;

  const endpoint = sub.endpoint;
  const ok = await sub.unsubscribe();
  // Tell the backend to drop the row even if the browser unsubscribe failed —
  // we don't want stale rows hanging around.
  try {
    await fetch(UNSUBSCRIBE_PROXY, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint }),
    });
  } catch {
    // best-effort
  }
  return ok;
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
