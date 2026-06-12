// Firebase Admin bootstrap. The service-account key lives ONLY in this
// container; the worker never touches Firestore or GCS directly.
//
// Credential resolution order:
//   1. FIREBASE_SERVICE_ACCOUNT      — inline JSON string
//   2. GOOGLE_APPLICATION_CREDENTIALS — path to the mounted service.json
//   3. Application Default Credentials (GCE metadata) as a last resort.

import {
    initializeApp,
    cert,
    applicationDefault,
    type App,
} from "firebase-admin/app";
import { getFirestore, FieldValue } from "firebase-admin/firestore";
import { getStorage } from "firebase-admin/storage";
import { config } from "./config";

function init(): App {
    const inline = process.env.FIREBASE_SERVICE_ACCOUNT;
    const keyPath = process.env.GOOGLE_APPLICATION_CREDENTIALS;

    const credential = inline
        ? cert(JSON.parse(inline))
        : keyPath
          ? cert(keyPath)
          : applicationDefault();

    return initializeApp({
        credential,
        ...(config.gcsBucket ? { storageBucket: config.gcsBucket } : {}),
    });
}

const app = init();

export const db = getFirestore(app);
export const bucket = getStorage(app).bucket();
export { FieldValue };
