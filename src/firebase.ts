// api/src/firebase.ts
//
// firebase-admin init. Credentials come from GOOGLE_APPLICATION_CREDENTIALS
// (a path to a service-account JSON) or the FIREBASE_SERVICE_ACCOUNT env var
// (the JSON itself, handy for containers). Storage bucket from config.gcsBucket.

import admin from "firebase-admin";
import { config } from "./config";

if (!admin.apps.length) {
    const inlineSa = process.env.FIREBASE_SERVICE_ACCOUNT;
    const options: admin.AppOptions = {};

    if (inlineSa) {
        options.credential = admin.credential.cert(JSON.parse(inlineSa));
    } else {
        // Falls back to GOOGLE_APPLICATION_CREDENTIALS / ADC.
        options.credential = admin.credential.applicationDefault();
    }
    if (config.gcsBucket) options.storageBucket = config.gcsBucket;

    admin.initializeApp(options);
    console.log("[firebase] initialized");
}

export const db = admin.firestore();
export const FieldValue = admin.firestore.FieldValue;
export const storage = admin.storage();
export { admin };
