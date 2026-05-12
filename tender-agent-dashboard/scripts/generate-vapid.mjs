#!/usr/bin/env node
// Generate a VAPID keypair for Web Push.
//
// Usage: npm run generate-vapid
//
// Copy the public key into NEXT_PUBLIC_VAPID_PUBLIC_KEY (dashboard .env.local)
// and the matching private key into VAPID_PRIVATE_KEY in the backend .env.
// VAPID_SUBJECT is a contact mailto: or https URL the push service can reach
// you on if a subscription misbehaves.

import webpush from "web-push";

const keys = webpush.generateVAPIDKeys();

console.log("# Add to tender-agent-dashboard/.env.local");
console.log(`NEXT_PUBLIC_VAPID_PUBLIC_KEY=${keys.publicKey}`);
console.log("");
console.log("# Add to tender-agent/.env");
console.log(`VAPID_PUBLIC_KEY=${keys.publicKey}`);
console.log(`VAPID_PRIVATE_KEY=${keys.privateKey}`);
console.log("VAPID_SUBJECT=mailto:admin@example.com");
