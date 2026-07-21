// api/src/lib/gcs.ts
//
// Upload a base64 image (PNG by default) to the configured GCS bucket and
// return a time-limited signed read URL. Used for captcha, QR, and receipt.

import { storage } from "../firebase";
import { config } from "../config";

export interface UploadResult {
    url: string;
    gcsPath: string;
    bytes: number;
    expiresAt: Date;
}

export async function uploadImage(opts: {
    base64: string;
    destination: string; // path under the bucket prefix, e.g. "<rid>_<driver>/qr_code.png"
    contentType?: string;
    signedUrlTtlSeconds: number;
}): Promise<UploadResult> {
    if (!config.gcsBucket) {
        throw new Error("GCS_BUCKET is not configured");
    }
    const contentType = opts.contentType ?? "image/png";
    const clean = opts.base64.replace(/^data:[^;]+;base64,/, "");
    const buffer = Buffer.from(clean, "base64");

    const gcsPath = `${config.gcsPrefix}/${opts.destination}`.replace(/\/+/g, "/");
    const file = storage.bucket().file(gcsPath);

    await file.save(buffer, {
        contentType,
        resumable: false,
        metadata: { cacheControl: "no-cache" },
    });

    const expiresAt = new Date(Date.now() + opts.signedUrlTtlSeconds * 1000);
    const [url] = await file.getSignedUrl({
        action: "read",
        expires: expiresAt,
    });

    return { url, gcsPath, bytes: buffer.length, expiresAt };
}
